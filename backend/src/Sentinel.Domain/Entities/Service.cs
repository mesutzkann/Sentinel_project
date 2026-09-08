namespace Sentinel.Domain.Entities;

/// <summary>
/// A monitored microservice.
/// </summary>
/// <remarks>
/// <see cref="Name"/> is the join key across every observability backend: it is the
/// <c>service.name</c> resource attribute in OTel, the Loki stream label, the Prometheus job and
/// the Jaeger service. The agent correlates signals by this string, so it must match the value a
/// service reports about itself.
/// </remarks>
public sealed class Service
{
    public Guid Id { get; set; } = Guid.NewGuid();

    public required string Name { get; set; }

    public required string DisplayName { get; set; }

    /// <summary>Path to the service's git repository, so git-mcp and source-code-mcp know where to look.</summary>
    public string? RepoPath { get; set; }

    /// <summary>Absolute health endpoint, polled by the dashboard before Prometheus exists.</summary>
    public string? HealthUrl { get; set; }

    /// <summary>Prometheus job label, when it differs from <see cref="Name"/>.</summary>
    public string? MetricsJob { get; set; }

    public DateTimeOffset CreatedAt { get; set; } = DateTimeOffset.UtcNow;

    public List<Incident> Incidents { get; set; } = [];
}
