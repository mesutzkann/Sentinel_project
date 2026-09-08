using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics.HealthChecks;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.Hosting;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Common.Health;

namespace Sentinel.Samples.Common;

/// <summary>
/// Everything the five sample services configure identically. Keeping it here means a sample
/// service's <c>Program.cs</c> stays short enough to read in one screen, which matters because
/// source-code-mcp will be reading these files during investigations.
/// </summary>
public static class ServiceDefaults
{
    /// <summary>
    /// Wires up JSON conventions, problem details, the chaos registry and health checks.
    /// OpenTelemetry is added in Phase 2 by <c>AddSentinelTelemetry</c>.
    /// </summary>
    /// <param name="serviceName">
    /// Logical service name, e.g. <c>orders</c>. Must match <c>sentinel.services.name</c> in the
    /// backend and the <c>service.name</c> resource attribute used by Loki, Prometheus and
    /// Jaeger — the agent correlates signals across all three by this string.
    /// </param>
    public static WebApplicationBuilder AddSampleServiceDefaults(
        this WebApplicationBuilder builder,
        string serviceName,
        params ChaosScenario[] scenarios)
    {
        builder.Services.AddSingleton(new SampleServiceInfo(serviceName));
        builder.Services.AddSingleton(new ChaosRegistry(scenarios));

        builder.Services.ConfigureHttpJsonOptions(options =>
        {
            options.SerializerOptions.PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower;
            options.SerializerOptions.DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull;
            options.SerializerOptions.Converters.Add(new JsonStringEnumConverter());
        });

        builder.Services.AddProblemDetails();

        builder.Services.AddHealthChecks()
            .AddCheck("self", () => HealthCheckResult.Healthy(), tags: ["live"]);

        return builder;
    }

    /// <summary>
    /// Registers the service's own <see cref="DbContext"/> against its dedicated schema, plus a
    /// readiness check that actually opens a connection.
    /// </summary>
    /// <remarks>
    /// The readiness check is what makes chaos scenario 9 (WRONG_CONNECTION_STRING) visible as a
    /// degraded service rather than only as request failures.
    /// </remarks>
    public static WebApplicationBuilder AddSampleDbContext<TContext>(
        this WebApplicationBuilder builder,
        string schema)
        where TContext : DbContext
    {
        var connectionString = builder.Configuration.GetConnectionString("Postgres")
            ?? throw new InvalidOperationException(
                "ConnectionStrings:Postgres is not configured. See .env.example.");

        builder.Services.AddDbContext<TContext>(options =>
            options.UseNpgsql(connectionString, npgsql =>
            {
                // Each sample service owns exactly one schema; its migration history table lives
                // there too, so the four services never contend over a shared history table.
                npgsql.MigrationsHistoryTable("__ef_migrations_history", schema);
            }));

        builder.Services.AddHealthChecks()
            .AddCheck<DbConnectivityHealthCheck<TContext>>(
                "database", tags: ["ready"]);

        return builder;
    }

    /// <summary>Maps <c>/health/live</c>, <c>/health/ready</c> and the chaos control endpoints.</summary>
    public static WebApplication MapSampleServiceDefaults(this WebApplication app)
    {
        app.MapHealthChecks("/health/live", new HealthCheckOptions
        {
            Predicate = check => check.Tags.Contains("live"),
            ResponseWriter = HealthResponseWriter.WriteAsync,
        });

        app.MapHealthChecks("/health/ready", new HealthCheckOptions
        {
            Predicate = check => check.Tags.Contains("ready"),
            ResponseWriter = HealthResponseWriter.WriteAsync,
        });

        // Aggregate view, used by the backend's service dashboard.
        app.MapHealthChecks("/health", new HealthCheckOptions
        {
            ResponseWriter = HealthResponseWriter.WriteAsync,
        });

        app.MapChaosEndpoints();

        app.MapGet("/", (SampleServiceInfo info) => Results.Ok(new
        {
            service = info.Name,
            status = "up",
        })).ExcludeFromDescription();

        return app;
    }

    /// <summary>
    /// Applies pending migrations at startup.
    /// </summary>
    /// <remarks>
    /// Acceptable only because these are disposable sample services on a local stack: it keeps
    /// <c>docker compose up</c> to a single command with no migration step. The real backend does
    /// not do this.
    /// </remarks>
    public static async Task MigrateSampleDatabaseAsync<TContext>(this WebApplication app)
        where TContext : DbContext
    {
        await using var scope = app.Services.CreateAsyncScope();
        var context = scope.ServiceProvider.GetRequiredService<TContext>();
        await context.Database.MigrateAsync();
    }
}

/// <summary>The service's logical name, injected wherever it is needed.</summary>
public sealed record SampleServiceInfo(string Name);
