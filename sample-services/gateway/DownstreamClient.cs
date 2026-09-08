using System.Text.Json;
using System.Text.Json.Nodes;
using Sentinel.Samples.Common;

namespace Sentinel.Samples.Gateway;

/// <summary>
/// The gateway's view of the other four services. One <see cref="HttpClient"/> is shared across
/// them because chaos scenario 8 works by changing a single timeout and observing which calls
/// break.
/// </summary>
public sealed class DownstreamClient
{
    private readonly HttpClient _http;
    private readonly IConfiguration _configuration;
    private readonly ILogger<DownstreamClient> _logger;

    public DownstreamClient(HttpClient http, IConfiguration configuration, ILogger<DownstreamClient> logger)
    {
        _http = http;
        _configuration = configuration;
        _logger = logger;
    }

    private string Users => _configuration["Services:Users"] ?? "http://localhost:8081";

    private string Orders => _configuration["Services:Orders"] ?? "http://localhost:8082";

    private string Payments => _configuration["Services:Payments"] ?? "http://localhost:8083";

    private string Notifications => _configuration["Services:Notifications"] ?? "http://localhost:8084";

    public async Task<JsonNode?> GetUserAsync(Guid userId, CancellationToken cancellationToken)
    {
        var response = await _http.GetAsync($"{Users}/users/{userId}", cancellationToken);

        return response.IsSuccessStatusCode
            ? await ReadJsonAsync(response, cancellationToken)
            : null;
    }

    public async Task<JsonNode?> GetOrderAsync(Guid orderId, CancellationToken cancellationToken)
    {
        var response = await _http.GetAsync($"{Orders}/orders/{orderId}", cancellationToken);

        return response.IsSuccessStatusCode
            ? await ReadJsonAsync(response, cancellationToken)
            : null;
    }

    public async Task<DownstreamResult> CreateOrderAsync(
        Guid userId,
        IEnumerable<object> items,
        string? currency,
        CancellationToken cancellationToken)
    {
        var response = await _http.PostAsJsonAsync(
            $"{Orders}/orders",
            new { UserId = userId, Items = items, Currency = currency },
            SampleJson.Options,
            cancellationToken);

        var body = await ReadJsonAsync(response, cancellationToken);

        if (!response.IsSuccessStatusCode)
        {
            _logger.LogWarning(
                "Order creation for {UserId} returned {StatusCode}", userId, (int)response.StatusCode);
        }

        return new DownstreamResult((int)response.StatusCode, body);
    }

    /// <summary>
    /// Polls every service's <c>/health</c> concurrently. A service that is down produces an
    /// entry with its error rather than failing the whole call — the dashboard needs to show
    /// four green and one red, not one exception.
    /// </summary>
    public async Task<IReadOnlyList<ServiceHealth>> GetEstateHealthAsync(CancellationToken cancellationToken)
    {
        var targets = new[]
        {
            ("users", Users),
            ("orders", Orders),
            ("payments", Payments),
            ("notifications", Notifications),
        };

        var checks = targets.Select(async target =>
        {
            var (name, baseUrl) = target;

            try
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                timeout.CancelAfter(TimeSpan.FromSeconds(3));

                var response = await _http.GetAsync($"{baseUrl}/health", timeout.Token);
                var body = await ReadJsonAsync(response, timeout.Token);

                return new ServiceHealth(
                    name,
                    baseUrl,
                    response.IsSuccessStatusCode ? "Healthy" : "Unhealthy",
                    body?["checks"],
                    null);
            }
            catch (Exception ex)
            {
                return new ServiceHealth(name, baseUrl, "Unreachable", null, ex.Message);
            }
        });

        return await Task.WhenAll(checks);
    }

    private static async Task<JsonNode?> ReadJsonAsync(
        HttpResponseMessage response,
        CancellationToken cancellationToken)
    {
        var raw = await response.Content.ReadAsStringAsync(cancellationToken);

        if (string.IsNullOrWhiteSpace(raw))
        {
            return null;
        }

        try
        {
            return JsonNode.Parse(raw);
        }
        catch (JsonException)
        {
            // A non-JSON body is itself evidence — an HTML error page from a proxy, say.
            return JsonValue.Create(raw);
        }
    }
}

public sealed record DownstreamResult(int StatusCode, JsonNode? Body);

public sealed record ServiceHealth(
    string Name,
    string BaseUrl,
    string Status,
    JsonNode? Checks,
    string? Error);
