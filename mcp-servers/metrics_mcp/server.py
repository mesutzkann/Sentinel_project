"""metrics-mcp: how the services behaved, from Prometheus.

Metrics are the signal that survives when logs do not. Slow queries, N+1 queries, retry storms
and CPU saturation all produce nothing in a log and a clear shape here — and the *combination*
of latency and error rate is what separates several otherwise identical failures:

    high latency + timeouts        connection pool exhaustion
    high latency + no errors       slow query, N+1, CPU saturation
    low latency  + high errors     circuit breaker stuck open
    unchanged latency + errors     a code defect on a subset of requests
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from _shared.config import settings
from _shared.results import window_label
from _shared.server import build_server, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "metrics-mcp",
    instructions=(
        "Prometheus metrics for the sample services: request rate, error rate, latency "
        "percentiles, CPU and memory. Services are identified by the `job` label, which is the "
        "service name. Use this to establish the shape of a failure — latency and error rate "
        "together usually narrow the cause to one or two candidates before any log is read."
    ),
)

# The collector stamps job from service.name, so `job="orders"` is the service. Same string the
# backend stores as services.metrics_job.
_JOB = "job"

# Duration histogram emitted by ASP.NET Core instrumentation.
_DURATION = "http_server_request_duration_seconds"

# Gauges from Sentinel.Samples.Common's ProcessMetrics. OTel appends _ratio to a unit-1 gauge.
_CPU = "process_cpu_utilization_ratio"
_WORKING_SET = "process_memory_working_set_bytes"

_GC_HEAP = "process_runtime_dotnet_gc_heap_size_bytes"


class PrometheusError(RuntimeError):
    """Prometheus could not answer."""


async def _instant(expr: str) -> list[dict[str, Any]]:
    """Runs an instant PromQL query and returns its result vector."""
    config = settings()

    try:
        async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as client:
            response = await client.get(
                f"{config.prometheus_url}/api/v1/query", params={"query": expr}
            )
    except httpx.HTTPError as exc:
        raise PrometheusError(
            f"Could not reach Prometheus at {config.prometheus_url}: {exc}"
        ) from exc

    body = response.json()

    if response.status_code >= 400 or body.get("status") != "success":
        raise PrometheusError(
            f"Prometheus rejected the query: {body.get('error', response.text[:300])}"
        )

    return body.get("data", {}).get("result", [])


async def _scalar(expr: str) -> float | None:
    """A single number, or None when the series does not exist.

    None and 0.0 mean different things and both occur: "this service reports no CPU metric" is
    not "this service is using no CPU", and an investigation that conflates them concludes the
    wrong thing.
    """
    results = await _instant(expr)

    if not results:
        return None

    return float(results[0]["value"][1])


def _by_job(results: list[dict[str, Any]]) -> dict[str, float]:
    return {r["metric"].get(_JOB, "unknown"): float(r["value"][1]) for r in results}


# --------------------------------------------------------------------------- tools ----


@read_only(
    server,
    "Request rate, error rate, latency percentiles, CPU and memory for one service in one "
    "call. The right first call when a service is suspected: it establishes the shape of the "
    "failure before you spend calls on detail.",
)
async def get_service_metrics(service: str, minutes: int = 15) -> dict[str, Any]:
    """Args:
    service: Service name, matching the Prometheus `job` label.
    minutes: Window for the rates and percentiles.
    """
    window = f"{minutes}m"
    selector = f'{{{_JOB}="{service}"}}'
    rate_selector = f'{{{_JOB}="{service}"}}[{window}]'

    total = await _scalar(f"sum(rate({_DURATION}_count{rate_selector}))")
    errors = await _scalar(
        f'sum(rate({_DURATION}_count{{{_JOB}="{service}",'
        f'http_response_status_code=~"5.."}}[{window}]))'
    )
    p95 = await _scalar(
        f"histogram_quantile(0.95, sum by (le) (rate({_DURATION}_bucket{rate_selector})))"
    )
    p99 = await _scalar(
        f"histogram_quantile(0.99, sum by (le) (rate({_DURATION}_bucket{rate_selector})))"
    )
    cpu = await _scalar(f"avg({_CPU}{selector})")
    memory = await _scalar(f"avg({_WORKING_SET}{selector})")

    if total is None:
        return {
            "service": service,
            "window": window_label(minutes),
            "note": (
                f"No metrics for job='{service}'. Either the service is not running, it is not "
                "exporting to the collector, or the name is wrong."
            ),
        }

    return {
        "service": service,
        "window": window_label(minutes),
        "request_rate_per_second": round(total, 4),
        "error_rate": round(errors / total, 4) if errors is not None and total > 0 else 0.0,
        "latency_p95_seconds": round(p95, 4) if p95 is not None else None,
        "latency_p99_seconds": round(p99, 4) if p99 is not None else None,
        "cpu_utilization": round(cpu, 4) if cpu is not None else None,
        "memory_working_set_mb": round(memory / 1_048_576, 1) if memory is not None else None,
        "interpretation": _shape(
            error_rate=(errors / total) if errors is not None and total > 0 else 0.0,
            p95=p95,
        ),
    }


def _shape(error_rate: float, p95: float | None) -> str:
    """Names the latency/error combination, which is most of the diagnosis."""
    slow = p95 is not None and p95 > 0.5
    failing = error_rate > 0.01

    if slow and failing:
        return (
            "Slow and failing. Consistent with connection pool exhaustion or a downstream "
            "dependency that has become slow enough to time out."
        )
    if slow:
        return (
            "Slow but not failing. Consistent with a slow query, an N+1 query pattern, or CPU "
            "saturation. Logs will likely show nothing; look at traces and the database."
        )
    if failing:
        return (
            "Failing without being slow. Consistent with a code defect on a subset of requests "
            "or a circuit breaker that is refusing work immediately."
        )

    return "Healthy on both axes over this window."


@read_only(
    server,
    "CPU utilisation, 0 to 1, where 1 is one machine's full capacity. Sustained values above "
    "0.9 with normal memory and no database involvement point at a compute problem rather "
    "than a waiting one.",
)
async def get_cpu_usage(service: str | None = None, minutes: int = 15) -> dict[str, Any]:
    """Args:
    service: One service, or omit for all of them.
    minutes: Averaging window.
    """
    selector = f'{{{_JOB}="{service}"}}' if service else ""
    results = await _instant(f"avg_over_time({_CPU}{selector}[{minutes}m])")

    if not results:
        return {
            "window": window_label(minutes),
            "note": (
                "No CPU series. The service exports it through Sentinel.Samples.Common; a "
                "missing series usually means the service is not running."
            ),
        }

    return {
        "window": window_label(minutes),
        "unit": "share of one machine's CPU, 0 to 1",
        "services": {job: round(value, 4) for job, value in sorted(_by_job(results).items())},
    }


@read_only(
    server,
    "Working set and GC heap. The distinguishing signal for a leak is the *shape*: memory that "
    "climbs monotonically without a plateau, rather than stepping up and holding.",
)
async def get_memory_usage(service: str | None = None, minutes: int = 60) -> dict[str, Any]:
    """Args:
    service: One service, or omit for all of them.
    minutes: Window over which the trend is measured.
    """
    selector = f'{{{_JOB}="{service}"}}' if service else ""
    current = _by_job(await _instant(f"{_WORKING_SET}{selector}"))

    if not current:
        return {"window": window_label(minutes), "note": "No memory series for that selector."}

    # Growth over the window, so the answer says whether memory is climbing rather than only
    # how much there is. A leak is a slope, not a level.
    earlier = _by_job(await _instant(f"{_WORKING_SET}{selector} offset {minutes}m"))
    heap = _by_job(await _instant(f"{_GC_HEAP}{selector}"))

    services = {}

    for job, value in sorted(current.items()):
        before = earlier.get(job)
        growth = None if before is None or before == 0 else round((value - before) / before, 4)

        services[job] = {
            "working_set_mb": round(value / 1_048_576, 1),
            "gc_heap_mb": round(heap[job] / 1_048_576, 1) if job in heap else None,
            "growth_over_window": growth,
        }

    return {
        "window": window_label(minutes),
        "services": services,
        "note": (
            "growth_over_window is the fractional change across the window. Sustained positive "
            "growth with no plateau is the leak signature; a single step is a load change. "
            "null means there was no data that far back."
        ),
    }


@read_only(
    server,
    "Requests per second per service. Use it to spot amplification: a downstream service whose "
    "rate jumps while its caller's rate does not is being retried, not called more.",
)
async def get_request_rate(service: str | None = None, minutes: int = 15) -> dict[str, Any]:
    """Args:
    service: One service, or omit for all of them.
    minutes: Rate window.
    """
    selector = f'{{{_JOB}="{service}"}}' if service else ""
    results = await _instant(f"sum by ({_JOB}) (rate({_DURATION}_count{selector}[{minutes}m]))")

    if not results:
        return {"window": window_label(minutes), "note": "No request series for that selector."}

    return {
        "window": window_label(minutes),
        "unit": "requests per second",
        "services": {job: round(value, 4) for job, value in sorted(_by_job(results).items())},
    }


@read_only(
    server,
    "Share of requests answered 5xx, per service. This is the request-level figure; logs-mcp's "
    "tool of the same name measures the share of log lines, which is a different thing and "
    "can disagree.",
)
async def get_error_rate(service: str | None = None, minutes: int = 15) -> dict[str, Any]:
    """Args:
    service: One service, or omit for all of them.
    minutes: Rate window.
    """
    job_filter = f'{_JOB}="{service}",' if service else ""
    window = f"{minutes}m"

    errors = _by_job(
        await _instant(
            f"sum by ({_JOB}) (rate({_DURATION}_count"
            f'{{{job_filter}http_response_status_code=~"5.."}}[{window}]))'
        )
    )
    totals = _by_job(
        await _instant(
            f"sum by ({_JOB}) (rate({_DURATION}_count"
            f"{{{job_filter.rstrip(',')}}}[{window}]))"
        )
    )

    if not totals:
        return {"window": window_label(minutes), "note": "No request series for that selector."}

    return {
        "window": window_label(minutes),
        # Services with no errors are listed as 0.0 rather than omitted: "payments is failing
        # and orders is not" is the comparison that localises a fault, and it needs both.
        "services": {
            job: round(errors.get(job, 0.0) / total, 4) if total > 0 else 0.0
            for job, total in sorted(totals.items())
        },
    }


@read_only(
    server,
    "Latency percentiles in seconds. p50 against p99 says whether everything is slow or only "
    "the tail — a p99 far above p50 usually means contention or a timeout, not uniform load.",
)
async def get_response_time(service: str | None = None, minutes: int = 15) -> dict[str, Any]:
    """Args:
    service: One service, or omit for all of them.
    minutes: Window for the histogram rate.
    """
    selector = f'{{{_JOB}="{service}"}}' if service else "{}"
    window = f"{minutes}m"

    percentiles: dict[str, dict[str, float]] = {}

    for label, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        results = await _instant(
            f"histogram_quantile({quantile}, sum by ({_JOB}, le) "
            f"(rate({_DURATION}_bucket{selector}[{window}])))"
        )

        for job, value in _by_job(results).items():
            # NaN comes back for a job with no observations in the window; it is not a latency
            # of zero and must not be reported as one.
            if value == value:
                percentiles.setdefault(job, {})[label] = round(value, 4)

    if not percentiles:
        return {"window": window_label(minutes), "note": "No latency series for that selector."}

    return {
        "window": window_label(minutes),
        "unit": "seconds",
        "services": dict(sorted(percentiles.items())),
    }


# Anything that mutates, records, or reaches outside a read. Prometheus's HTTP API is read-only
# by nature, but the query language can still be used to hammer it, and the agent is an
# untrusted author of these strings.
_FORBIDDEN = re.compile(
    r"\b(drop|delete|alter|admin|tsdb|clean_tombstones|snapshot)\b", re.IGNORECASE
)


@read_only(
    server,
    "Run a PromQL expression directly, for questions the other tools do not cover. Prefer the "
    "specific tools: they return interpreted results, this returns raw series.",
)
async def query_promql(expr: str, minutes: int = 15) -> dict[str, Any]:
    """Args:
    expr: A PromQL expression. Use `[Xm]` ranges inside it as needed.
    minutes: Advisory only, reported back so the caller can see what window was intended.
    """
    if _FORBIDDEN.search(expr):
        return {
            "error": "Rejected: the expression contains an administrative keyword.",
            "expr": expr,
        }

    if len(expr) > 1000:
        return {"error": "Rejected: expression is longer than 1000 characters.", "expr": expr}

    results = await _instant(expr)
    limit = settings().max_results

    return {
        "expr": expr,
        "window": window_label(minutes),
        "found": len(results),
        "series": [
            {"labels": r["metric"], "value": r["value"][1]} for r in results[:limit]
        ],
        "truncated": len(results) > limit,
    }


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
