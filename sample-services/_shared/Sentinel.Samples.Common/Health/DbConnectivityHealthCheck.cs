using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Diagnostics.HealthChecks;

namespace Sentinel.Samples.Common.Health;

/// <summary>
/// Readiness check that opens a real connection rather than inspecting configuration.
/// </summary>
/// <remarks>
/// Written by hand instead of pulling in <c>AspNetCore.HealthChecks.NpgSql</c> — one fewer
/// dependency, and the failure message is ours to shape. The exception message is deliberately
/// surfaced: for chaos scenario 9 it is the evidence that names the unreachable host.
/// </remarks>
public sealed class DbConnectivityHealthCheck<TContext> : IHealthCheck
    where TContext : DbContext
{
    private readonly TContext _context;

    public DbConnectivityHealthCheck(TContext context) => _context = context;

    public async Task<HealthCheckResult> CheckHealthAsync(
        HealthCheckContext context,
        CancellationToken cancellationToken = default)
    {
        try
        {
            var canConnect = await _context.Database.CanConnectAsync(cancellationToken);

            return canConnect
                ? HealthCheckResult.Healthy("Database reachable.")
                : HealthCheckResult.Unhealthy("Database unreachable.");
        }
        catch (Exception ex)
        {
            return HealthCheckResult.Unhealthy($"Database unreachable: {ex.Message}", ex);
        }
    }
}
