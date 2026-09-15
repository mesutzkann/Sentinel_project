using System.Text.Json.Nodes;
using Sentinel.Samples.Common.Chaos;

namespace Sentinel.Samples.Users;

/// <summary>
/// Reads a user's orders from the orders service, for the profile endpoint.
/// </summary>
/// <remarks>
/// The only outbound call this service makes, and it exists so that chaos scenario 10
/// (RETRY_STORM) has an amplifier. A retry storm needs a caller and a callee; users had neither
/// before this, which is why the scenario could be declared but not implemented.
/// </remarks>
public sealed class OrdersClient
{
    private readonly HttpClient _http;
    private readonly ChaosRegistry _chaos;
    private readonly ILogger<OrdersClient> _logger;

    public OrdersClient(HttpClient http, ChaosRegistry chaos, ILogger<OrdersClient> logger)
    {
        _http = http;
        _chaos = chaos;
        _logger = logger;
    }

    /// <summary>
    /// The orders belonging to one user.
    /// </summary>
    /// <remarks>
    /// Chaos scenario 10 is the loop below, and the bug it stands for is a real one: a retry that
    /// never checks whether it already succeeded. The healthy path makes the call once; with the
    /// scenario on it makes it <c>attempts</c> times, keeps the last answer, and returns it —
    /// every one of those calls succeeding.
    ///
    /// **That is what makes it an amplifier rather than an outage.** Nothing fails, latency per
    /// request rises only by the extra round trips, and the only thing that changes sharply is
    /// how much traffic orders receives for the same traffic into users. The catalogue's
    /// discriminator — downstream load rises while upstream load does not — is exactly this shape,
    /// and it is why `search_code` is one of the tools the scenario is diagnosed with: a loop
    /// with no exit on success is visible in the source and nowhere else.
    /// </remarks>
    public async Task<JsonNode?> GetForUserAsync(Guid userId, CancellationToken cancellationToken)
    {
        var attempts = _chaos.IsEnabled(ChaosCodes.RetryStorm)
            ? _chaos.GetInt(ChaosCodes.RetryStorm, "attempts", 10)
            : 1;

        var backoff = _chaos.GetInt(ChaosCodes.RetryStorm, "backoff_ms", 0);

        JsonNode? orders = null;

        for (var attempt = 1; attempt <= attempts; attempt++)
        {
            var response = await _http.GetAsync(
                $"/orders?userId={userId}&limit=50", cancellationToken);

            if (response.IsSuccessStatusCode)
            {
                var raw = await response.Content.ReadAsStringAsync(cancellationToken);
                orders = string.IsNullOrWhiteSpace(raw) ? null : JsonNode.Parse(raw);

                // The defect. A retry that has an answer should stop asking for it, and this one
                // never looks. Written as the mistake rather than as a switch, so that reading
                // the method is enough to see why orders is being called ten times.
            }
            else
            {
                _logger.LogWarning(
                    "Orders returned {StatusCode} for user {UserId} on attempt {Attempt}",
                    (int)response.StatusCode,
                    userId,
                    attempt);
            }

            if (backoff > 0 && attempt < attempts)
            {
                await Task.Delay(backoff, cancellationToken);
            }
        }

        return orders;
    }
}
