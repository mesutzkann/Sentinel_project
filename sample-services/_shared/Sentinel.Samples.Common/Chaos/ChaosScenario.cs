namespace Sentinel.Samples.Common.Chaos;

/// <summary>
/// One injectable failure mode, as catalogued in <c>sample-services/chaos/scenarios.md</c>.
/// A service declares the scenarios it owns at startup; the behaviour itself is implemented
/// inside the service, guarded by <see cref="ChaosRegistry.IsEnabled"/>.
/// </summary>
/// <param name="Code">
/// Stable identifier, e.g. <c>DB_CONNECTION_POOL_EXHAUSTION</c>. This is also the value stored
/// in <c>sentinel.root_causes.category</c>, so it must not change once evaluation data exists.
/// </param>
/// <param name="Title">Short human-readable name, shown in the frontend.</param>
/// <param name="Description">What actually breaks when this is enabled.</param>
/// <param name="DefaultParameters">
/// Parameters the caller may override on enable, e.g. <c>delay_ms</c>. Keys are documented per
/// scenario; unknown keys are kept so a service can read whatever it declared.
/// </param>
public sealed record ChaosScenario(
    string Code,
    string Title,
    string Description,
    IReadOnlyDictionary<string, string>? DefaultParameters = null);
