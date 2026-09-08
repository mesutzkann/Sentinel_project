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
}
