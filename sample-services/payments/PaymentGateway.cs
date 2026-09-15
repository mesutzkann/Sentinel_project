using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Common.Telemetry;

namespace Sentinel.Samples.Payments;

/// <summary>
/// Thrown when the breaker is open and the call was never attempted.
/// </summary>
/// <remarks>
/// Named after Polly's exception rather than something of this project's own, because the name is
/// evidence: it is what appears in the log line the agent searches for, and an engineer reading
/// <c>BrokenCircuitException</c> knows immediately that the call did not happen. The project does
/// not depend on Polly — a breaker whose whole point is to get stuck is clearer written out than
/// configured, and scenario 13 needs the half-open probe to fail on purpose.
/// </remarks>
public sealed class BrokenCircuitException : Exception
{
    public BrokenCircuitException(string message) : base(message)
    {
    }
}

/// <summary>
/// The outbound call to the card network, behind a circuit breaker.
/// </summary>
/// <remarks>
/// There is no real provider — see <see cref="PaymentProcessor"/> — but the breaker in front of
/// it is real, and it is what chaos scenario 13 (CIRCUIT_BREAKER_STUCK_OPEN) breaks. Nothing here
/// simulates being stuck: the breaker opens because calls genuinely failed, and it stays open
/// because the half-open probe genuinely fails too, which is exactly how a real breaker behaves
/// while its dependency is still down.
///
/// The signature that makes this scenario recognisable is that the failures are <em>fast</em>.
/// Once the breaker is open a request spends no time on the network and returns in single-digit
/// milliseconds, so the combination of a high error rate with unusually low latency is unique to
/// this scenario in the catalogue. It is what separates it from scenario 1, where failures are
/// timeouts, and from scenario 11, where nothing fails at all.
/// </remarks>
public sealed class PaymentGateway
{
    /// <summary>How long the breaker stays open before letting one probe through.</summary>
    /// <remarks>
    /// Short, so an evaluation run sees several open/probe/re-open cycles inside its window
    /// rather than one. Each failed probe logs, which is what makes "the probe never succeeds"
    /// visible in Loki instead of merely true.
    /// </remarks>
    private static readonly TimeSpan OpenDuration = TimeSpan.FromSeconds(10);

    private readonly ChaosRegistry _chaos;
    private readonly ILogger<PaymentGateway> _logger;
    private readonly object _gate = new();

    private int _consecutiveFailures;
    private DateTimeOffset? _openedAt;

    public PaymentGateway(ChaosRegistry chaos, ILogger<PaymentGateway> logger)
    {
        _chaos = chaos;
        _logger = logger;
    }

    /// <summary>
    /// Authorises against the card network, or fails fast when the breaker is open.
    /// </summary>
    /// <exception cref="BrokenCircuitException">The breaker is open; nothing was called.</exception>
    public async Task AuthorizeAsync(Guid orderId, CancellationToken cancellationToken)
    {
        HealIfProviderRecovered();

        if (IsOpen())
        {
            // No network, no waiting. This is the branch that produces the scenario's
            // discriminator, and it has to stay cheap for that to be true.
            throw new BrokenCircuitException(
                $"The circuit for payment-gateway is open and order {orderId} was not attempted.");
        }

        using var activity = SampleActivity.StartInternal("payments.gateway.authorize");

        try
        {
            await CallProviderAsync(cancellationToken);
        }
        catch (Exception)
        {
            RecordFailure();
            throw;
        }

        RecordSuccess();
    }

    /// <summary>
    /// The provider call. Fails while scenario 13 is enabled, succeeds otherwise.
    /// </summary>
    /// <remarks>
    /// The failure is immediate rather than a timeout, because this scenario is about a
    /// dependency that refuses rather than one that hangs — the hanging case is scenario 12, and
    /// the two would be indistinguishable if this waited.
    /// </remarks>
    private async Task CallProviderAsync(CancellationToken cancellationToken)
    {
        // Stands in for the round trip to the card network on the healthy path.
        await Task.Delay(TimeSpan.FromMilliseconds(4), cancellationToken);

        if (_chaos.IsEnabled(ChaosCodes.CircuitBreakerStuckOpen))
        {
            throw new HttpRequestException("payment-gateway refused the connection.");
        }
    }

    /// <summary>
    /// Closes the breaker as soon as the provider is healthy again, without waiting out the
    /// open window.
    /// </summary>
    /// <remarks>
    /// This exists because of a measurement. Driving the scenario, disabling it, and immediately
    /// measuring a baseline gave 39.7% failures with the scenario off: the breaker was still
    /// inside its ten-second open window and was refusing requests that would have succeeded.
    /// Ten seconds is comparable to the twelve the benchmark spends on a baseline, so the next
    /// case in an evaluation run would have measured the previous case's breaker and called a
    /// healthy service broken.
    ///
    /// It is not a special case for the benchmark. A breaker whose dependency has recovered
    /// *should* close, and the only reason the probe has to wait at all is that probing costs a
    /// request against something that is probably still down. Here the service knows it is not.
    ///
    /// None of this makes the breaker easier for the agent to fix: the agent has no way to
    /// disable a chaos scenario, so while the scenario is on the provider is still refusing and
    /// the breaker still reopens behind every probe.
    /// </remarks>
    private void HealIfProviderRecovered()
    {
        if (_chaos.IsEnabled(ChaosCodes.CircuitBreakerStuckOpen))
        {
            return;
        }

        lock (_gate)
        {
            if (_openedAt is null && _consecutiveFailures == 0)
            {
                return;
            }

            _openedAt = null;
            _consecutiveFailures = 0;

            _logger.LogInformation(
                "Circuit breaker for payment-gateway closed: the provider is answering again");
        }
    }

    private bool IsOpen()
    {
        lock (_gate)
        {
            if (_openedAt is not { } openedAt)
            {
                return false;
            }

            if (DateTimeOffset.UtcNow - openedAt < OpenDuration)
            {
                return true;
            }

            // Half-open: this one request is allowed through as a probe. While the dependency is
            // still failing it will fail too, and RecordFailure will open the breaker again —
            // which is the whole of "stuck open". No branch here knows about the scenario.
            _openedAt = null;
            _logger.LogWarning(
                "Circuit breaker for payment-gateway is half-open, probing with one request");

            return false;
        }
    }

    private void RecordFailure()
    {
        lock (_gate)
        {
            _consecutiveFailures++;

            var threshold = _chaos.GetInt(ChaosCodes.CircuitBreakerStuckOpen, "failure_threshold", 5);

            if (_consecutiveFailures < threshold || _openedAt is not null)
            {
                return;
            }

            _openedAt = DateTimeOffset.UtcNow;

            // The line scenario 13 is found by. Searched for rather than counted, so the wording
            // matters as much as the level.
            _logger.LogError(
                "Circuit breaker opened for payment-gateway after {Failures} consecutive failures",
                _consecutiveFailures);
        }
    }

    private void RecordSuccess()
    {
        lock (_gate)
        {
            if (_consecutiveFailures > 0)
            {
                _logger.LogInformation(
                    "Circuit breaker for payment-gateway closed after a successful call");
            }

            _consecutiveFailures = 0;
            _openedAt = null;
        }
    }
}
