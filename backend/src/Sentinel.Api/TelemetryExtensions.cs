using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;
using Serilog;
using Serilog.Sinks.OpenTelemetry;

namespace Sentinel.Api;

/// <summary>
/// OpenTelemetry for the backend itself.
/// </summary>
/// <remarks>
/// SentinelAI investigates services through Loki, Prometheus and Jaeger, so it reports into the
/// same three. That is not symmetry for its own sake: from Phase 7 an investigation spans the
/// frontend, this API and the AI service, and a trace that stops at the API boundary cannot show
/// where an investigation actually spent its time. It is also what the self-observability
/// dashboard in Phase 11 is built on.
/// </remarks>
public static class TelemetryExtensions
{
    private const string ServiceName = "sentinel-backend";

    /// <summary>
    /// Adds tracing, metrics and OTLP log export when an endpoint is configured.
    /// </summary>
    /// <remarks>
    /// Silent when <c>OTEL_EXPORTER_OTLP_ENDPOINT</c> is unset, so the backend still runs against
    /// the core profile alone. Console logging is unaffected either way — it is the sink a
    /// developer actually watches.
    /// </remarks>
    public static WebApplicationBuilder AddSentinelTelemetry(this WebApplicationBuilder builder)
    {
        if (string.IsNullOrWhiteSpace(OtlpEndpoint(builder.Configuration)))
        {
            return builder;
        }

        var resource = ResourceBuilder.CreateDefault()
            .AddService(ServiceName)
            .AddAttributes([new KeyValuePair<string, object>("deployment.environment", "local")]);

        builder.Services.AddOpenTelemetry()
            .WithTracing(tracing => tracing
                .SetResourceBuilder(resource)
                .AddAspNetCoreInstrumentation(options =>
                {
                    options.Filter = context =>
                        !context.Request.Path.StartsWithSegments("/health");

                    options.RecordException = true;
                })
                .AddHttpClientInstrumentation(options => options.RecordException = true)
                // EF Core's provider spans, so a slow dashboard query is attributable to a
                // statement rather than to "the backend".
                .AddSource("Npgsql")
                .AddOtlpExporter())
            .WithMetrics(metrics => metrics
                .SetResourceBuilder(resource)
                .AddAspNetCoreInstrumentation()
                .AddHttpClientInstrumentation()
                .AddRuntimeInstrumentation()
                .AddOtlpExporter());

        return builder;
    }

    /// <summary>Adds the OTLP sink to Serilog, alongside the console.</summary>
    public static LoggerConfiguration WriteToOtlp(
        this LoggerConfiguration configuration,
        IConfiguration appConfiguration)
    {
        var endpoint = OtlpEndpoint(appConfiguration);

        if (string.IsNullOrWhiteSpace(endpoint))
        {
            return configuration;
        }

        return configuration.WriteTo.OpenTelemetry(options =>
        {
            // Serilog's sink does not append the per-signal path the way the OTel SDK does, so
            // the logs endpoint is spelled out. Protocol has to match it: the base endpoint is
            // gRPC by convention here, and gRPC ignores the path.
            options.Endpoint = endpoint;
            options.Protocol = OtlpProtocol.Grpc;

            // Same service.name the traces and metrics carry. This is the string every
            // cross-signal query joins on.
            options.ResourceAttributes = new Dictionary<string, object>
            {
                ["service.name"] = ServiceName,
                ["deployment.environment"] = "local",
            };
        });
    }

    private static string? OtlpEndpoint(IConfiguration configuration) =>
        configuration["OTEL_EXPORTER_OTLP_ENDPOINT"];
}
