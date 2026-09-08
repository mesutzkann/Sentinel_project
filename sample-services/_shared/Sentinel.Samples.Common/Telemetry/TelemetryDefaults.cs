using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using OpenTelemetry;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace Sentinel.Samples.Common.Telemetry;

/// <summary>
/// Traces, metrics and logs for a sample service, all leaving over OTLP to the collector.
/// </summary>
/// <remarks>
/// The three signals share one <see cref="ResourceBuilder"/>, so <c>service.name</c> is identical
/// across them. That is the whole point: the agent correlates a Loki stream, a Prometheus series
/// and a Jaeger trace by that one string, and a mismatch would silently break every
/// cross-signal investigation.
/// </remarks>
public static class TelemetryDefaults
{
    /// <summary>
    /// Wires up OpenTelemetry when an OTLP endpoint is configured, and does nothing when it is
    /// not.
    /// </summary>
    /// <remarks>
    /// Opting out on an empty endpoint is deliberate. The observability profile is separate from
    /// the samples profile, so a service started without it would otherwise spend every export
    /// cycle retrying against a collector that is not running, filling its own logs with the
    /// failure and drowning the signal a developer is actually looking at.
    /// </remarks>
    public static WebApplicationBuilder AddSentinelTelemetry(
        this WebApplicationBuilder builder,
        string serviceName)
    {
        // Read only to decide whether to export at all. The endpoint itself is left to the SDK,
        // which reads the same variable along with OTEL_EXPORTER_OTLP_PROTOCOL and appends the
        // per-signal path — behaviour that setting Endpoint by hand would bypass, and the usual
        // way to end up exporting gRPC at an HTTP port.
        var endpoint = builder.Configuration["OTEL_EXPORTER_OTLP_ENDPOINT"];

        if (string.IsNullOrWhiteSpace(endpoint))
        {
            return builder;
        }

        var resource = ResourceBuilder.CreateDefault()
            .AddService(serviceName)
            .AddAttributes([new KeyValuePair<string, object>("deployment.environment", "local")]);

        builder.Logging.AddOpenTelemetry(options =>
        {
            options.SetResourceBuilder(resource);

            // Without these the log body reaches Loki as a template with the arguments dropped,
            // so "Order {OrderId} paid" would arrive with no order id in it.
            options.IncludeFormattedMessage = true;
            options.IncludeScopes = true;
            options.ParseStateValues = true;

            options.AddOtlpExporter();
        });

        // Singleton so the CPU gauge keeps its previous sample between observations, and so the
        // Meter outlives the request that first touched it.
        builder.Services.AddSingleton<ProcessMetrics>();

        builder.Services.AddOpenTelemetry()
            .WithTracing(tracing => tracing
                .SetResourceBuilder(resource)
                .AddAspNetCoreInstrumentation(options =>
                {
                    // Health probes run every 15 seconds against five services. Left in, they
                    // are most of the trace volume and none of the signal.
                    options.Filter = context =>
                        !context.Request.Path.StartsWithSegments("/health");

                    options.RecordException = true;
                })
                .AddHttpClientInstrumentation(options => options.RecordException = true)
                // Npgsql's own spans, subscribed by source name rather than through
                // Npgsql.OpenTelemetry's AddNpgsql(), whose name collides with the EF Core
                // extension of the same name. Scenario 2 (one slow query) and scenario 4 (fifty
                // fast ones) are indistinguishable without a span per statement.
                .AddSource("Npgsql")
                .AddOtlpExporter())
            .WithMetrics(metrics => metrics
                .SetResourceBuilder(resource)
                .AddAspNetCoreInstrumentation()
                .AddHttpClientInstrumentation()
                // Gen-2 collections and heap size, which is what makes scenario 7 visible as a
                // monotonic climb rather than as a sudden OutOfMemoryException.
                .AddRuntimeInstrumentation()
                // CPU utilisation and working set. The runtime instrumentation above reports
                // neither, so without this metrics-mcp cannot answer get_cpu_usage at all and
                // scenario 15 has no metric signal.
                .AddMeter(ProcessMetrics.MeterName)
                .AddOtlpExporter());

        return builder;
    }
}
