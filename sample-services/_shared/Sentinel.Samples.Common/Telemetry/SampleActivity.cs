using System.Diagnostics;

namespace Sentinel.Samples.Common.Telemetry;

/// <summary>
/// The sample services' own span source, for work that happens inside a service.
/// </summary>
/// <remarks>
/// ASP.NET Core and Npgsql instrumentation between them describe every span these services
/// produced before this existed: a server span, an outgoing HTTP span, a database span. That
/// covers most of the catalogue, and it is deliberately all most scenarios get.
///
/// It does not cover compute. Scenario 15 (CPU_SATURATION) is defined by time going somewhere
/// that is neither the database nor the network, and a trace made only of instrumented I/O shows
/// that as a slow server span with nothing underneath it — the right latency attached to no
/// explanation. <c>get_slowest_spans</c> would return the endpoint, which is the symptom the
/// investigation started from.
///
/// One source for all five services rather than one each: the span name says what the work was,
/// the <c>service.name</c> resource attribute already says where it ran, and a per-service source
/// would mean five registrations in <see cref="TelemetryDefaults"/> that can each be forgotten.
/// </remarks>
public static class SampleActivity
{
    /// <summary>Registered with the tracer provider in <see cref="TelemetryDefaults"/>.</summary>
    public const string Name = "Sentinel.Samples";

    private static readonly ActivitySource Source = new(Name);

    /// <summary>
    /// Starts an internal span, or returns null when nothing is listening.
    /// </summary>
    /// <remarks>
    /// Null is the normal case for a service started without the observability profile, and
    /// callers are expected to <c>using</c> the result rather than check it: disposing a null
    /// Activity is a no-op, which keeps the chaos code free of telemetry branching.
    /// </remarks>
    public static Activity? StartInternal(string name) =>
        Source.StartActivity(name, ActivityKind.Internal);
}
