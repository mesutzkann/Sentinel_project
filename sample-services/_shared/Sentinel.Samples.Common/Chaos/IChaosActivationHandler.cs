namespace Sentinel.Samples.Common.Chaos;

/// <summary>
/// Work a service needs to do when one of its scenarios is switched on or off.
/// </summary>
/// <remarks>
/// Most scenarios are a branch in a request handler and need nothing here. This exists for the
/// ones that have to prepare the world first — scenario 2 has to grow the orders table to a size
/// where a sequential scan over it actually costs something, and doing that lazily inside the
/// first request would leave one request wearing a multi-second setup cost that has nothing to
/// do with the fault being demonstrated.
///
/// Handlers must be idempotent: <c>enable</c> can be called any number of times, and agent
/// evaluation depends on that.
/// </remarks>
public interface IChaosActivationHandler
{
    /// <summary>Called after a scenario is enabled, before the endpoint responds.</summary>
    Task OnEnabledAsync(string code, CancellationToken cancellationToken);

    /// <summary>Called after a scenario is disabled or reset, before the endpoint responds.</summary>
    /// <remarks>
    /// A default no-op, because almost nothing needs it: a scenario that is a branch in a request
    /// handler stops happening the moment the flag is off.
    ///
    /// What needs it is a scenario that reached outside this process. Scenario 12 tells a
    /// third-party provider to start refusing, and nothing about switching the flag off would
    /// tell it to stop — the provider would still be down while the next benchmark case measured
    /// a "healthy" baseline against it. That is the same failure the payments circuit breaker
    /// had, where state outliving its scenario made the following case measure the previous one.
    ///
    /// It does not make a scenario easier for the agent to fix, because the agent has no way to
    /// disable a chaos scenario. This is the harness putting the world back.
    /// </remarks>
    Task OnDisabledAsync(string code, CancellationToken cancellationToken) => Task.CompletedTask;
}
