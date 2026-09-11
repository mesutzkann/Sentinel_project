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
