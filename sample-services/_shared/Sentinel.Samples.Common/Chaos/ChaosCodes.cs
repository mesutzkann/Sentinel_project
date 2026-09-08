namespace Sentinel.Samples.Common.Chaos;

/// <summary>
/// The fifteen scenario codes from <c>sample-services/chaos/scenarios.md</c>.
/// </summary>
/// <remarks>
/// Shared rather than declared per service because the codes are a cross-cutting contract: they
/// name a scenario for the agent evaluation dataset, for <c>root_causes.category</c> in the
/// backend, and — for the ones implemented in shared infrastructure such as the connection pool —
/// for code that runs inside every service regardless of which one owns the scenario.
/// </remarks>
public static class ChaosCodes
{
    // A. Database
    public const string DbConnectionPoolExhaustion = "DB_CONNECTION_POOL_EXHAUSTION";
    public const string DbSlowQueryMissingIndex = "DB_SLOW_QUERY_MISSING_INDEX";
    public const string DbDeadlock = "DB_DEADLOCK";
    public const string DbNPlusOneQuery = "DB_N_PLUS_ONE_QUERY";

    // B. Code defects
    public const string NullReferenceException = "NULL_REFERENCE_EXCEPTION";
    public const string DivideByZeroEdgeCase = "DIVIDE_BY_ZERO_EDGE_CASE";
    public const string MemoryLeak = "MEMORY_LEAK";

    // C. Configuration
    public const string TimeoutTooLow = "TIMEOUT_TOO_LOW";
    public const string WrongConnectionString = "WRONG_CONNECTION_STRING";
    public const string RetryStorm = "RETRY_STORM";

    // D. Dependency and cascade
    public const string DownstreamLatencyCascade = "DOWNSTREAM_LATENCY_CASCADE";
    public const string ExternalDependencyUnavailable = "EXTERNAL_DEPENDENCY_UNAVAILABLE";
    public const string CircuitBreakerStuckOpen = "CIRCUIT_BREAKER_STUCK_OPEN";

    // E. Deployment and resources
    public const string BadDeploymentRegression = "BAD_DEPLOYMENT_REGRESSION";
    public const string CpuSaturation = "CPU_SATURATION";
}
