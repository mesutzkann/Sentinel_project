using System.Collections.Concurrent;

namespace Sentinel.Samples.Common.Chaos;

/// <summary>
/// Tracks which chaos scenarios are currently active in this process.
///
/// State is in-memory and per-process on purpose: restarting a service is also a reset, which is
/// what makes <c>restart_container</c> a meaningful remediation in Phase 10 and keeps agent
/// evaluation runs reproducible.
///
/// Registered as a singleton. All members are thread-safe; scenarios are toggled from HTTP
/// requests while request handlers read them concurrently.
/// </summary>
public sealed class ChaosRegistry
{
    private readonly IReadOnlyDictionary<string, ChaosScenario> _declared;
    private readonly ConcurrentDictionary<string, IReadOnlyDictionary<string, string>> _active = new();

    public ChaosRegistry(IEnumerable<ChaosScenario> declared)
    {
        _declared = declared.ToDictionary(s => s.Code, StringComparer.OrdinalIgnoreCase);
    }

    /// <summary>Scenarios this service owns, whether or not they are currently enabled.</summary>
    public IReadOnlyCollection<ChaosScenario> Declared => _declared.Values.ToArray();

    public bool IsDeclared(string code) => _declared.ContainsKey(code);

    /// <summary>
    /// Hot path: called from request handlers, so it stays a single dictionary lookup.
    /// </summary>
    public bool IsEnabled(string code) => _active.ContainsKey(code);

    /// <summary>
    /// Reads a parameter of an enabled scenario, falling back to the scenario's declared default
    /// and then to <paramref name="fallback"/>. Returns <paramref name="fallback"/> when the
    /// scenario is disabled, so callers can read parameters unconditionally.
    /// </summary>
    public int GetInt(string code, string key, int fallback)
    {
        if (!_active.TryGetValue(code, out var parameters))
        {
            return fallback;
        }

        if (parameters.TryGetValue(key, out var raw) && int.TryParse(raw, out var parsed))
        {
            return parsed;
        }

        if (_declared.TryGetValue(code, out var scenario)
            && scenario.DefaultParameters is { } defaults
            && defaults.TryGetValue(key, out var rawDefault)
            && int.TryParse(rawDefault, out var parsedDefault))
        {
            return parsedDefault;
        }

        return fallback;
    }

    /// <summary>
    /// Idempotent. Enabling an already-enabled scenario replaces its parameters, which lets an
    /// evaluation run retune a scenario without a disable/enable cycle.
    /// </summary>
    /// <returns><c>false</c> when this service does not own the scenario.</returns>
    public bool Enable(string code, IReadOnlyDictionary<string, string>? parameters = null)
    {
        if (!_declared.TryGetValue(code, out var scenario))
        {
            return false;
        }

        var merged = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (key, value) in scenario.DefaultParameters ?? new Dictionary<string, string>())
        {
            merged[key] = value;
        }

        foreach (var (key, value) in parameters ?? new Dictionary<string, string>())
        {
            merged[key] = value;
        }

        _active[scenario.Code] = merged;
        return true;
    }

    /// <summary>Idempotent. Disabling a scenario that is not enabled is a no-op.</summary>
    /// <returns><c>false</c> when this service does not own the scenario.</returns>
    public bool Disable(string code)
    {
        if (!_declared.TryGetValue(code, out var scenario))
        {
            return false;
        }

        _active.TryRemove(scenario.Code, out _);
        return true;
    }

    /// <summary>Disables everything. Called before each agent evaluation run.</summary>
    public void Reset() => _active.Clear();

    /// <summary>Current state of every declared scenario, for <c>GET /chaos</c>.</summary>
    public IReadOnlyCollection<ChaosScenarioState> Snapshot() =>
        _declared.Values
            .Select(s => new ChaosScenarioState(
                s.Code,
                s.Title,
                s.Description,
                _active.TryGetValue(s.Code, out var p),
                p ?? s.DefaultParameters ?? new Dictionary<string, string>()))
            .OrderBy(s => s.Code, StringComparer.Ordinal)
            .ToArray();
}

/// <summary>A scenario plus whether it is currently active. Response shape of <c>GET /chaos</c>.</summary>
public sealed record ChaosScenarioState(
    string Code,
    string Title,
    string Description,
    bool Enabled,
    IReadOnlyDictionary<string, string> Parameters);
