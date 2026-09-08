"""traces-mcp: where the time went and which service actually failed, from Jaeger.

Traces answer the question logs and metrics cannot: *which* service is the cause when several
look sick. Three of the fifteen chaos scenarios put the cause in a different service from the
symptom, and one — the N+1 query — has no signature at all except the shape of a trace.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from typing import Any

import httpx

from _shared.config import settings
from _shared.results import empty, result, truncated, window_label
from _shared.server import build_server, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "traces-mcp",
    instructions=(
        "Distributed traces from Jaeger. Use this to follow one request across services and "
        "see where its time went. Traces are decisive when several services look unhealthy at "
        "once: the slow span is the cause and its callers are merely waiting. They are also "
        "the only way to see a request that issues fifty small database queries instead of one."
    ),
)


class JaegerError(RuntimeError):
    """Jaeger could not answer."""


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    config = settings()

    try:
        async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as client:
            response = await client.get(f"{config.jaeger_url}{path}", params=params)
    except httpx.HTTPError as exc:
        raise JaegerError(f"Could not reach Jaeger at {config.jaeger_url}: {exc}") from exc

    if response.status_code >= 400:
        raise JaegerError(f"Jaeger returned {response.status_code}: {response.text[:300]}")

    return response.json()


async def _search(
    service: str,
    minutes: int,
    limit: int,
    tags: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "service": service,
        "limit": limit,
        "lookback": f"{minutes}m",
        # Jaeger wants microseconds since epoch for the window bounds.
        "end": int(time.time() * 1_000_000),
        "start": int((time.time() - minutes * 60) * 1_000_000),
    }

    if tags:
        # Jaeger takes tags as a JSON object in a query parameter.
        params["tags"] = json.dumps(tags)

    body = await _get("/api/traces", params)
    return body.get("data") or []


def _summarise(trace: dict[str, Any]) -> dict[str, Any]:
    """One line per trace: what it was, how long, and whether it failed."""
    spans = trace.get("spans", [])
    processes = trace.get("processes", {})

    if not spans:
        return {"trace_id": trace.get("traceID"), "spans": 0}

    root = min(spans, key=lambda s: s["startTime"])
    services = {
        processes.get(s.get("processID"), {}).get("serviceName", "unknown") for s in spans
    }
    errors = [s for s in spans if _is_error(s)]

    return {
        "trace_id": trace.get("traceID"),
        "operation": root.get("operationName"),
        "root_service": processes.get(root.get("processID"), {}).get("serviceName", "unknown"),
        "duration_ms": round(max(s["duration"] for s in spans) / 1000, 2),
        "span_count": len(spans),
        "services": sorted(services),
        "error_count": len(errors),
        "failing_services": sorted(
            {processes.get(s.get("processID"), {}).get("serviceName", "unknown") for s in errors}
        ),
    }


def _is_error(span: dict[str, Any]) -> bool:
    """Whether a span is marked failed.

    Checks both `error=true` and `otel.status_code=ERROR`: the .NET SDK sets the latter, and
    Jaeger's own UI convention is the former. Missing one silently halves the failed traces.
    """
    for tag in span.get("tags", []):
        key, value = tag.get("key"), tag.get("value")

        if key == "error" and value in (True, "true"):
            return True
        if key == "otel.status_code" and value == "ERROR":
            return True
        if key == "http.response.status_code" and str(value).startswith("5"):
            return True

    return False


# --------------------------------------------------------------------------- tools ----


@read_only(
    server,
    "Recent traces for a service, newest first. Each entry summarises which services took "
    "part, how long the request took, and whether anything failed — enough to pick one trace "
    "worth opening in full.",
)
async def get_recent_traces(service: str, minutes: int = 15, limit: int = 10) -> dict[str, Any]:
    """Args:
    service: Service that started or took part in the trace.
    minutes: How far back to look.
    limit: Maximum traces to return.
    """
    traces = await _search(service, minutes, min(limit, settings().max_results))
    window = window_label(minutes)

    if not traces:
        return empty(window, f"traces involving {service}", service=service)

    items = sorted(
        (_summarise(t) for t in traces), key=lambda s: s.get("duration_ms", 0), reverse=True
    )

    return result(found=len(items), window=window, items=items, service=service)


@read_only(
    server,
    "Only the traces that contain a failed span. The `failing_services` field is the important "
    "one: when it names a service other than the one you searched, the fault is downstream of "
    "the symptom.",
)
async def get_failed_traces(service: str, minutes: int = 15, limit: int = 10) -> dict[str, Any]:
    """Args:
    service: Service that started or took part in the trace.
    minutes: How far back to look.
    limit: Maximum traces to return.
    """
    # Fetched wider than `limit` and filtered here rather than with a tag query: a trace can
    # fail in a span belonging to another service, and a server-side tag filter on this service
    # would miss exactly the cross-service failures that matter most.
    traces = await _search(service, minutes, min(limit * 10, 200))
    window = window_label(minutes)

    failed = [_summarise(t) for t in traces if any(_is_error(s) for s in t.get("spans", []))]

    if not failed:
        return empty(
            window,
            f"failed traces involving {service}",
            service=service,
            interpretation=(
                f"No trace involving {service} contains a failed span. If the service is "
                "measurably unhealthy, the failure is one that does not mark a span as an "
                "error — slowness, for example."
            ),
        )

    shown, was_truncated = truncated(
        sorted(failed, key=lambda s: s.get("duration_ms", 0), reverse=True), limit
    )

    return result(
        found=len(failed),
        window=window,
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        service=service,
        scanned=len(traces),
    )


@read_only(
    server,
    "Every span of one trace, in order, with its duration and parent. This is where an N+1 "
    "query becomes visible as dozens of sibling database spans, and where a timeout shows the "
    "caller failing while the callee succeeded.",
)
async def get_trace_details(trace_id: str) -> dict[str, Any]:
    """Args:
    trace_id: The trace identifier, as returned by the other tools.
    """
    body = await _get(f"/api/traces/{trace_id}")
    data = body.get("data") or []

    if not data:
        return {"trace_id": trace_id, "found": 0, "note": "No trace with that id."}

    trace = data[0]
    processes = trace.get("processes", {})
    spans = sorted(trace.get("spans", []), key=lambda s: s["startTime"])
    start = spans[0]["startTime"] if spans else 0

    items = [
        {
            "span_id": s.get("spanID"),
            "service": processes.get(s.get("processID"), {}).get("serviceName", "unknown"),
            "operation": s.get("operationName"),
            # Relative to the trace start, which is what makes the sequence readable.
            "offset_ms": round((s["startTime"] - start) / 1000, 2),
            "duration_ms": round(s["duration"] / 1000, 2),
            "error": _is_error(s),
            "parent_span_id": next(
                (
                    ref.get("spanID")
                    for ref in s.get("references", [])
                    if ref.get("refType") == "CHILD_OF"
                ),
                None,
            ),
        }
        for s in spans
    ]

    # Sibling spans on the same operation are the N+1 signature, so they are counted rather than
    # left for the model to notice among fifty near-identical lines.
    repeated = Counter(f"{i['service']}:{i['operation']}" for i in items)
    suspicious = {k: v for k, v in repeated.items() if v >= 10}

    summary = _summarise(trace)

    return {
        "trace_id": trace_id,
        "summary": summary,
        "spans": items,
        "repeated_operations": suspicious or None,
        "note": (
            "One operation repeats many times within a single trace, which is the signature of "
            "a query issued per item rather than once."
            if suspicious
            else None
        ),
    }


@read_only(
    server,
    "The slowest spans across recent traces, grouped by operation. Answers 'what is this "
    "service actually waiting on' — a database span, an outbound HTTP span, or its own compute.",
)
async def get_slowest_spans(service: str, minutes: int = 15, limit: int = 10) -> dict[str, Any]:
    """Args:
    service: Service to examine.
    minutes: How far back to look.
    limit: Maximum operations to return.
    """
    traces = await _search(service, minutes, 100)
    window = window_label(minutes)

    if not traces:
        return empty(window, f"traces involving {service}", service=service)

    durations: dict[str, list[float]] = {}
    owner: dict[str, str] = {}

    for trace in traces:
        processes = trace.get("processes", {})

        for span in trace.get("spans", []):
            name = span.get("operationName", "unknown")
            durations.setdefault(name, []).append(span["duration"] / 1000)
            owner[name] = processes.get(span.get("processID"), {}).get("serviceName", "unknown")

    items = sorted(
        (
            {
                "operation": name,
                "service": owner[name],
                "count": len(values),
                "max_ms": round(max(values), 2),
                "mean_ms": round(sum(values) / len(values), 2),
                "total_ms": round(sum(values), 2),
            }
            for name, values in durations.items()
        ),
        # Ranked by total rather than max: one very slow span and a thousand slightly slow ones
        # are different problems, and total time is what an investigation is trying to account
        # for.
        key=lambda item: item["total_ms"],
        reverse=True,
    )

    shown, was_truncated = truncated(items, limit)

    return result(
        found=len(items),
        window=window,
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        service=service,
        ranked_by="total time across traces",
    )


@read_only(
    server,
    "Which services call which, derived from traces. Use it to work out where a failure can "
    "propagate from and to before deciding which service to look at next.",
)
async def get_service_dependencies(minutes: int = 60) -> dict[str, Any]:
    """Args:
    minutes: Window over which call edges are collected.
    """
    window = window_label(minutes)

    body = await _get(
        "/api/dependencies",
        {"endTs": int(time.time() * 1000), "lookback": minutes * 60 * 1000},
    )

    edges = body.get("data") or []

    if not edges:
        # Jaeger's dependency graph is produced by a batch job that all-in-one does not run, so
        # an empty answer here is expected rather than a fault. Derived from traces instead.
        return await _dependencies_from_traces(minutes)

    return {
        "window": window,
        "found": len(edges),
        "edges": [
            {"caller": e.get("parent"), "callee": e.get("child"), "calls": e.get("callCount")}
            for e in edges
        ],
        "source": "jaeger dependency graph",
    }


async def _dependencies_from_traces(minutes: int) -> dict[str, Any]:
    """Rebuilds the call graph by walking recent traces."""
    services = await _get("/api/services")
    names = [s for s in (services.get("data") or []) if s != "jaeger-all-in-one"]

    edges: Counter[tuple[str, str]] = Counter()

    for name in names:
        for trace in await _search(name, minutes, 20):
            processes = trace.get("processes", {})
            spans = {s["spanID"]: s for s in trace.get("spans", [])}

            for span in spans.values():
                caller_id = next(
                    (
                        ref.get("spanID")
                        for ref in span.get("references", [])
                        if ref.get("refType") == "CHILD_OF"
                    ),
                    None,
                )

                if caller_id is None or caller_id not in spans:
                    continue

                caller = processes.get(spans[caller_id].get("processID"), {}).get("serviceName")
                callee = processes.get(span.get("processID"), {}).get("serviceName")

                if caller and callee and caller != callee:
                    edges[(caller, callee)] += 1

    return {
        "window": window_label(minutes),
        "found": len(edges),
        "edges": [
            {"caller": caller, "callee": callee, "calls": count}
            for (caller, callee), count in edges.most_common(settings().max_results)
        ],
        "source": "derived from traces",
        "note": (
            "Jaeger all-in-one does not run the batch job that builds its dependency graph, so "
            "these edges were rebuilt by walking recent traces. Call counts are therefore a "
            "sample, not a total."
        ),
    }


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
