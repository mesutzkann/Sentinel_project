using System.Diagnostics;
using System.Diagnostics.Metrics;

namespace Sentinel.Samples.Common.Telemetry;

/// <summary>
/// CPU utilisation and working set for this process.
/// </summary>
/// <remarks>
/// Hand-written rather than taken from OpenTelemetry.Instrumentation.Process, which is still
/// pre-release: two numbers do not justify a release-candidate dependency in the one place the
/// agent looks to tell a compute problem from a memory one.
///
/// Both are load-bearing. Scenario 15 (CPU_SATURATION) is recognised by CPU pinned high while
/// the database and network stay quiet, and scenario 7 (MEMORY_LEAK) by a working set that
/// climbs monotonically rather than stepping. Without these, metrics-mcp could report neither
/// and both scenarios would be unsolvable from metrics alone.
/// </remarks>
public sealed class ProcessMetrics : IDisposable
{
    public const string MeterName = "Sentinel.Samples.Process";

    private readonly Meter _meter;
    private readonly Process _process = Process.GetCurrentProcess();
    private readonly object _gate = new();

    private TimeSpan _lastProcessorTime;
    private DateTime _lastSampledAt;

    public ProcessMetrics()
    {
        _lastProcessorTime = _process.TotalProcessorTime;
        _lastSampledAt = DateTime.UtcNow;

        _meter = new Meter(MeterName);

        _meter.CreateObservableGauge(
            "process.cpu.utilization",
            ObserveCpuUtilization,
            unit: "1",
            description: "Share of one machine's CPU capacity used by this process, 0 to 1.");

        _meter.CreateObservableGauge(
            "process.memory.working_set",
            () => _process.WorkingSet64,
            unit: "By",
            description: "Resident memory of this process.");
    }

    /// <summary>
    /// CPU used since the previous observation, divided by the wall clock and core count.
    /// </summary>
    /// <remarks>
    /// A delta rather than a total, because <see cref="Process.TotalProcessorTime"/> only ever
    /// climbs: exported as-is it would show a process using more CPU the longer it had been
    /// alive. Dividing by <see cref="Environment.ProcessorCount"/> keeps the value in 0..1 on
    /// any machine, so "above 90%" means the same thing on a laptop and in CI.
    ///
    /// Locked because the metrics SDK may observe from more than one thread, and two readers
    /// interleaving would give one of them a negative interval.
    /// </remarks>
    private double ObserveCpuUtilization()
    {
        lock (_gate)
        {
            _process.Refresh();

            var now = DateTime.UtcNow;
            var processorTime = _process.TotalProcessorTime;

            var elapsed = (now - _lastSampledAt).TotalSeconds;
            var used = (processorTime - _lastProcessorTime).TotalSeconds;

            _lastSampledAt = now;
            _lastProcessorTime = processorTime;

            // First observation, or two observations inside the clock's resolution.
            if (elapsed <= 0)
            {
                return 0;
            }

            var utilization = used / (elapsed * Environment.ProcessorCount);

            // Clamped because the two clocks are sampled independently and can disagree
            // slightly, which otherwise produces a utilisation just above 1.
            return Math.Clamp(utilization, 0, 1);
        }
    }

    public void Dispose()
    {
        _meter.Dispose();
        _process.Dispose();
    }
}
