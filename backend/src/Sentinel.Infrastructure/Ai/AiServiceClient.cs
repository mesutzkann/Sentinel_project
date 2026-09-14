using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using Sentinel.Application.Common;

namespace Sentinel.Infrastructure.Ai;

public sealed class AiServiceOptions
{
    public const string SectionName = "AiService";

    /// <summary>Where the Python service listens. Natively localhost; in compose, a container name.</summary>
    public string BaseUrl { get; set; } = "http://localhost:8000";

    /// <summary>
    /// This backend's address <em>as the AI service sees it</em>, which is what callback URLs are
    /// built from.
    /// </summary>
    /// <remarks>
    /// Not derived from the incoming request. The request that starts an investigation comes
    /// from a browser and carries the browser's view of this host — which is the wrong one the
    /// moment either process runs in a container, and produces a callback URL that resolves to
    /// nothing with no error until the first event is posted.
    /// </remarks>
    public string CallbackBaseUrl { get; set; } = "http://localhost:5080";

    /// <summary>
    /// How long to wait for the 202.
    /// </summary>
    /// <remarks>
    /// Short, because this is an acknowledgement and not the investigation: the AI service
    /// answers before it builds anything. A long timeout here would only make a dead service
    /// look like a slow one.
    /// </remarks>
    public int TimeoutSeconds { get; set; } = 15;
}

/// <summary>
/// Starts investigations over HTTP.
/// </summary>
/// <remarks>
/// The whole of the backend's outbound contract with the agent. Everything else the agent has to
/// say comes back through the callback surface, which is why this class has one method and no
/// polling: a client that asked "are you done yet" would be a second, worse copy of the timeline
/// the events already build.
/// </remarks>
public sealed class AiServiceClient : IAiServiceClient
{
    /// <summary>Named so the timeout and base address are configured once, at registration.</summary>
    public const string HttpClientName = "ai-service";

    /// <summary>
    /// How long an approved action may take. Generous, and deliberately so: the call runs the
    /// tool, waits out a settle window and re-measures the symptom before answering.
    /// </summary>
    public static readonly TimeSpan ExecuteTimeout = TimeSpan.FromMinutes(3);

    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly IHttpClientFactory _clients;
    private readonly AiServiceOptions _options;
    private readonly ILogger<AiServiceClient> _logger;

    public AiServiceClient(
        IHttpClientFactory clients,
        IOptions<AiServiceOptions> options,
        ILogger<AiServiceClient> logger)
    {
        _clients = clients;
        _options = options.Value;
        _logger = logger;
    }

    public async Task StartInvestigationAsync(
        StartInvestigationRequest request,
        CancellationToken cancellationToken = default)
    {
        var client = _clients.CreateClient(HttpClientName);

        var body = new
        {
            investigation_id = request.InvestigationId,

            // The contract's field name, carrying the incident *code*: the agent puts it in
            // prompts and on the timeline, and INC-00142 is what a person recognises.
            incident_id = request.IncidentCode,
            query = request.Query,
            service_hint = request.ServiceHint,
            callback_url = $"{_options.CallbackBaseUrl.TrimEnd('/')}{request.CallbackUrl}",
            callback_token = request.CallbackToken,
        };

        HttpResponseMessage response;

        try
        {
            response = await client.PostAsJsonAsync("/investigations", body, Json, cancellationToken);
        }
        catch (Exception exception) when (exception is HttpRequestException or TaskCanceledException)
        {
            throw new AiServiceUnavailableException(
                $"The AI service at {_options.BaseUrl} did not answer: {exception.Message}");
        }

        if (response.IsSuccessStatusCode)
        {
            _logger.LogInformation(
                "Investigation {InvestigationId} accepted by the AI service", request.InvestigationId);

            return;
        }

        var detail = await Detail(response, cancellationToken);

        if (response.StatusCode == HttpStatusCode.Conflict)
        {
            // The agent already has a run under this id. A conflict rather than an outage, and
            // the caller should hear the difference.
            throw new ConflictException($"The AI service is already investigating this: {detail}");
        }

        throw new AiServiceUnavailableException(
            $"The AI service refused the investigation with {(int)response.StatusCode}: {detail}");
    }

    public async Task<ExecuteActionResult> ExecuteActionAsync(
        ExecuteActionRequest request,
        CancellationToken cancellationToken = default)
    {
        var client = _clients.CreateClient(HttpClientName);

        // Its own timeout, because this call waits for a settle window and a re-measurement
        // rather than for an acknowledgement. The named client's timeout is sized for the
        // latter, and a remediation that is cut off halfway has still changed the system.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(ExecuteTimeout);

        var body = new
        {
            tool = request.Tool,
            arguments = Arguments(request.ArgumentsJson),
            approval_token = request.ApprovalToken,
            service = request.Service,
            investigation_id = request.InvestigationId,
        };

        HttpResponseMessage response;

        try
        {
            response = await client.PostAsJsonAsync(
                $"/actions/{request.RecommendationId}/execute", body, Json, timeout.Token);
        }
        catch (Exception exception) when (exception is HttpRequestException or TaskCanceledException)
        {
            throw new AiServiceUnavailableException(
                $"The AI service at {_options.BaseUrl} did not answer: {exception.Message}");
        }

        var payload = await response.Content.ReadAsStringAsync(cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            throw new AiServiceUnavailableException(
                $"The AI service refused the action ({(int)response.StatusCode}): {Trim(payload)}");
        }

        return Read(payload);
    }

    /// <summary>The execution response, read defensively: a field that moved must not lose the outcome.</summary>
    private static ExecuteActionResult Read(string payload)
    {
        using var document = JsonDocument.Parse(payload);
        var root = document.RootElement;
        var verification = root.TryGetProperty("verification", out var found)
            ? found
            : default;

        return new ExecuteActionResult(
            Executed: root.TryGetProperty("executed", out var executed) && executed.GetBoolean(),
            Confirmed: root.TryGetProperty("confirmed", out var confirmed) && confirmed.GetBoolean(),
            Verdict: Text(verification, "verdict") ?? "unknown",
            Summary: Text(verification, "summary") ?? string.Empty,
            Error: Text(root, "error"),

            // The whole body is kept: `recommendations.execution_result` is an audit record, and
            // a projection of it made by this method would be an audit of what this method
            // thought was interesting.
            RawJson: payload);
    }

    private static string? Text(JsonElement element, string property) =>
        element.ValueKind is JsonValueKind.Object
        && element.TryGetProperty(property, out var value)
        && value.ValueKind is JsonValueKind.String
            ? value.GetString()
            : null;

    /// <summary>The stored arguments as an object, or an empty one. Never null: the hash covers it.</summary>
    private static JsonElement Arguments(string? argumentsJson)
    {
        if (!string.IsNullOrWhiteSpace(argumentsJson))
        {
            try
            {
                using var parsed = JsonDocument.Parse(argumentsJson);

                if (parsed.RootElement.ValueKind is JsonValueKind.Object)
                {
                    return parsed.RootElement.Clone();
                }
            }
            catch (JsonException)
            {
                // Falls through to the empty object. A recommendation whose arguments will not
                // parse cannot be executed with them, and the approval hash will refuse it.
            }
        }

        using var empty = JsonDocument.Parse("{}");

        return empty.RootElement.Clone();
    }

    private static string Trim(string payload) =>
        payload.Length <= 400 ? payload : payload[..400];

    private static async Task<string> Detail(HttpResponseMessage response, CancellationToken cancellationToken)
    {
        try
        {
            var body = await response.Content.ReadAsStringAsync(cancellationToken);

            return body.Length > 300 ? body[..300] : body;
        }
        catch (Exception)
        {
            return response.ReasonPhrase ?? "no detail";
        }
    }
}
