using System.Collections.Concurrent;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Notifications.Domain;

namespace Sentinel.Samples.Notifications;

/// <summary>
/// Runtime behaviour behind the notifications scenarios that need state rather than a branch.
/// </summary>
/// <remarks>
/// Scenario 9 is not here: a wrong database host is a property of the connection, so it lives in
/// the shared data access setup beside the pool scenario rather than in a request handler.
/// </remarks>
public sealed class ChaosBehaviour : IChaosActivationHandler
{
    /// <summary>
    /// The leak. Every delivered notification is appended and nothing ever removes one.
    /// </summary>
    /// <remarks>
    /// An instance field on a singleton rather than a <c>static</c>, which is what the catalogue
    /// describes. The distinction does not matter to the fault — both live for the life of the
    /// process — and it does matter to the tests, where a static would leak between cases in the
    /// same test run.
    /// </remarks>
    private readonly ConcurrentBag<DeliveryRecord> _history = [];

    private readonly ChaosRegistry _chaos;
    private readonly IHttpClientFactory _clients;
    private readonly ILogger<ChaosBehaviour> _logger;

    public ChaosBehaviour(
        ChaosRegistry chaos,
        IHttpClientFactory clients,
        ILogger<ChaosBehaviour> logger)
    {
        _chaos = chaos;
        _clients = clients;
        _logger = logger;
    }

    /// <summary>How many deliveries are currently retained. Read by the tests.</summary>
    public int RetainedCount => _history.Count;

    /// <summary>
    /// Scenario 7 (MEMORY_LEAK): retains every delivery, so working set climbs and never falls.
    /// </summary>
    /// <remarks>
    /// The payload is what makes the leak measurable rather than merely true. A record of a few
    /// dozen bytes would need millions of requests to show up against a 320 MiB container limit;
    /// at the default below, a benchmark window of ten thousand deliveries triples the working
    /// set, which is a slope on a graph rather than noise.
    ///
    /// **This is the only fault in the catalogue whose signal is a shape over time.** Every other
    /// one steps, spikes or collapses; this one rises monotonically and keeps rising, and the
    /// gen-2 collection count rises with it because none of what it holds can ever be collected.
    /// That is why the scenario is recognised by a trend rather than by a threshold.
    /// </remarks>
    public void Retain(Notification notification)
    {
        // 2 KiB, and the number is measured against `docker stats` rather than against
        // Prometheus: a container that has been rebuilt leaves its old series behind in the
        // collector, and reading the maximum across them reported a plateau that belonged to a
        // previous instance. From a fresh restart, a 75 second window at 24 concurrent takes the
        // container from 51 MiB to 171 MiB — a 3.3x climb with 149 MiB still to spare.
        //
        // The headroom is deliberate. The catalogue's ending — silence, then an
        // OutOfMemoryException — is what a long run reaches, and a benchmark window stops short
        // of it on purpose, because a fault that kills its own service is a fault the next case
        // inherits.
        var bytes = _chaos.GetInt(ChaosCodes.MemoryLeak, "bytes_per_delivery", 2048);

        _history.Add(new DeliveryRecord(
            notification.Id,
            notification.Recipient,
            DateTimeOffset.UtcNow,
            new byte[bytes]));
    }

    /// <inheritdoc />
    /// <remarks>
    /// Scenario 12 is a fault in something else, so switching it on means telling that something
    /// to start refusing. The notifications service does not simulate a 503 — it makes the same
    /// call it always makes and receives one.
    /// </remarks>
    public Task OnEnabledAsync(string code, CancellationToken cancellationToken) =>
        code == ChaosCodes.ExternalDependencyUnavailable
            ? SetProviderAvailabilityAsync(unavailable: true, cancellationToken)
            : Task.CompletedTask;

    /// <inheritdoc />
    public Task OnDisabledAsync(string code, CancellationToken cancellationToken) =>
        code == ChaosCodes.ExternalDependencyUnavailable
            ? SetProviderAvailabilityAsync(unavailable: false, cancellationToken)
            : Task.CompletedTask;

    /// <summary>
    /// Tells the delivery provider to start or stop refusing.
    /// </summary>
    /// <remarks>
    /// Failures here are logged and swallowed. This is the harness arm of the scenario rather
    /// than the delivery path: if the provider cannot be reached to be told, the benchmark's
    /// reproduction check will report that the fault did not reproduce, which is a truer
    /// outcome than a 500 from the chaos endpoint that makes the run look like a code failure.
    /// </remarks>
    private async Task SetProviderAvailabilityAsync(
        bool unavailable,
        CancellationToken cancellationToken)
    {
        try
        {
            var client = _clients.CreateClient(ProviderControlClient);

            using var response = await client.PostAsJsonAsync(
                "/_control/availability",
                new { unavailable },
                cancellationToken);

            response.EnsureSuccessStatusCode();

            _logger.LogWarning(
                "Delivery provider availability set to {Available}",
                unavailable ? "unavailable" : "available");
        }
        catch (Exception exception)
        {
            _logger.LogError(
                exception,
                "Could not set the delivery provider's availability to {Unavailable}",
                unavailable);
        }
    }

    /// <summary>Named client for the provider's control plane, registered in Program.cs.</summary>
    public const string ProviderControlClient = "delivery-provider-control";

    /// <summary>
    /// Releases the retained history once the scenario is off.
    /// </summary>
    /// <remarks>
    /// The lesson from payments' circuit breaker, applied before it could bite twice: state that
    /// outlives the scenario that created it pollutes the next case's baseline. A hundred
    /// megabytes of retained deliveries would still be held while the following benchmark
    /// measured a "healthy" notifications, and the memory series it recorded would belong to the
    /// case before it.
    ///
    /// It makes the scenario no easier for the agent, which has no way to disable a chaos
    /// scenario — from its side the only thing that frees the memory is still
    /// <c>restart_container</c>, which is the mitigation the catalogue names.
    /// </remarks>
    public void DrainIfDisabled()
    {
        if (_chaos.IsEnabled(ChaosCodes.MemoryLeak) || _history.IsEmpty)
        {
            return;
        }

        var retained = _history.Count;
        _history.Clear();

        _logger.LogInformation(
            "Released {Count} retained delivery records after the scenario was disabled",
            retained);
    }

    /// <summary>One delivery, kept forever. The buffer is the weight.</summary>
    private sealed record DeliveryRecord(
        Guid NotificationId,
        string Recipient,
        DateTimeOffset DeliveredAt,
        byte[] Payload);
}
