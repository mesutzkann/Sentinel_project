using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics.HealthChecks;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.EntityFrameworkCore;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Npgsql;
using Sentinel.Samples.Common.Chaos;
using Sentinel.Samples.Common.Health;
using Sentinel.Samples.Common.Telemetry;

namespace Sentinel.Samples.Common;

/// <summary>
/// Everything the five sample services configure identically. Keeping it here means a sample
/// service's <c>Program.cs</c> stays short enough to read in one screen, which matters because
/// source-code-mcp will be reading these files during investigations.
/// </summary>
public static class ServiceDefaults
{
    /// <summary>
    /// Wires up JSON conventions, problem details, the chaos registry, health checks and
    /// OpenTelemetry.
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

        builder.AddSentinelTelemetry(serviceName);

        // EF logs every statement it runs at Information, which would put the text and duration
        // of each query into Loki. Scenarios 2 and 4 are built on the failing component emitting
        // no logs at all, so that one default would hand the agent the answer and collapse the
        // distinction between a log-only investigation and a real multi-signal one. The slow
        // query is still available where it belongs: PostgreSQL's own
        // log_min_duration_statement, the Npgsql spans, and pg_stat_statements.
        builder.Logging.AddFilter(
            "Microsoft.EntityFrameworkCore.Database.Command", LogLevel.Warning);

        builder.Services.ConfigureHttpJsonOptions(
            options => SampleJson.Apply(options.SerializerOptions));

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

        builder.Services.AddDbContext<TContext>((serviceProvider, options) =>
            options.UseNpgsql(
                ChaosConnectionString(
                    connectionString, serviceProvider.GetRequiredService<ChaosRegistry>()),
                npgsql =>
                {
                    // Each sample service owns exactly one schema; its migration history table
                    // lives there too, so the four services never contend over a shared history
                    // table.
                    npgsql.MigrationsHistoryTable("__ef_migrations_history", schema);
                }));

        builder.Services.AddHealthChecks()
            .AddCheck<DbConnectivityHealthCheck<TContext>>(
                "database", tags: ["ready"]);

        return builder;
    }

    /// <summary>
    /// Applies chaos scenario 1 (DB_CONNECTION_POOL_EXHAUSTION) to the connection string.
    /// </summary>
    /// <remarks>
    /// Lives in the shared layer rather than in orders because the pool belongs to the data
    /// access setup, not to a request handler. Only orders declares the scenario, and
    /// <see cref="ChaosRegistry.GetInt"/> returns the fallback for a scenario a service does not
    /// own, so this is inert everywhere else.
    ///
    /// Npgsql pools are keyed by connection string, so a changed string is a different pool: the
    /// scenario genuinely exhausts one rather than pretending to. The shortened timeout is what
    /// turns exhaustion into the timeout errors the scenario is recognised by, instead of
    /// requests queueing for the 15 second default.
    /// </remarks>
    private static string ChaosConnectionString(string connectionString, ChaosRegistry chaos)
    {
        if (!chaos.IsEnabled(ChaosCodes.DbConnectionPoolExhaustion))
        {
            return connectionString;
        }

        return new NpgsqlConnectionStringBuilder(connectionString)
        {
            MaxPoolSize = chaos.GetInt(ChaosCodes.DbConnectionPoolExhaustion, "max_pool_size", 20),
            Timeout = chaos.GetInt(ChaosCodes.DbConnectionPoolExhaustion, "timeout_seconds", 5),
        }.ConnectionString;
    }

    /// <summary>Maps <c>/health/live</c>, <c>/health/ready</c> and the chaos control endpoints.</summary>
    public static WebApplication MapSampleServiceDefaults(this WebApplication app)
    {
        // Resolved eagerly, not for its API but for its constructor: ProcessMetrics creates the
        // Meter and registers the CPU and working-set gauges there. A singleton nothing ever
        // asks for is never built, and the metrics would simply never appear — with no error
        // anywhere to say why.
        app.Services.GetService<ProcessMetrics>();

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
