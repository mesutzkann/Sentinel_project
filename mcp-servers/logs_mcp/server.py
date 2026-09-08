"""logs-mcp: what the services wrote, from Loki.

The single most over-trusted signal in an investigation. Six of the fifteen chaos scenarios
produce no error log from the failing component at all, so these tools are built to make an
empty result legible as evidence rather than as a dead end.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from typing import Any

import httpx

from _shared.config import settings
from _shared.results import empty, result, truncated, window_label
from _shared.server import build_server, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "logs-mcp",
    instructions=(
        "Structured logs from the sample services, stored in Loki. Use this to find what a "
        "service reported: exceptions, error bursts, and the messages around them. Remember "
        "that a service can be badly broken and log nothing at all — an empty result here is "
        "evidence that the fault is silent, not evidence that nothing is wrong."
    ),
)

# Loki labels the stream by OTel resource attributes. service.name becomes service_name, which
# is the same string the backend stores and Prometheus uses as `job`.
_SERVICE_LABEL = "service_name"

# Loki caps a single query response; asking for more just wastes the round trip.
_LOKI_HARD_LIMIT = 5000

# How many lines to pull when a tool needs the text itself rather than a count. Kept well under
# the hard limit: this is a sample for classification, and the exact totals come from _count.
_SAMPLE_LIMIT = 500


class LokiError(RuntimeError):
    """Loki could not answer."""


async def _query_range(query: str, minutes: int, limit: int) -> list[dict[str, Any]]:
    """Runs a LogQL range query and flattens the streams into entries."""
    config = settings()
    end_ns = time.time_ns()
    start_ns = end_ns - minutes * 60 * 1_000_000_000

    params = {
        "query": query,
        "start": str(start_ns),
        "end": str(end_ns),
        "limit": str(min(limit, _LOKI_HARD_LIMIT)),
        # Newest first: an investigation almost always wants the most recent occurrence.
        "direction": "backward",
    }

    try:
        async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as client:
            response = await client.get(
                f"{config.loki_url}/loki/api/v1/query_range", params=params
            )
    except httpx.HTTPError as exc:
        raise LokiError(f"Could not reach Loki at {config.loki_url}: {exc}") from exc

    if response.status_code >= 400:
        raise LokiError(f"Loki rejected the query ({response.status_code}): {response.text[:300]}")

    entries: list[dict[str, Any]] = []

    for stream in response.json().get("data", {}).get("result", []):
        labels = stream.get("stream", {})

        for timestamp_ns, line in stream.get("values", []):
            entries.append(
                {
                    "timestamp": _iso(int(timestamp_ns)),
                    "service": labels.get(_SERVICE_LABEL, "unknown"),
                    "level": labels.get("severity_text") or labels.get("detected_level", "unknown"),
                    "message": line,
                    # Present only on lines emitted inside a span. It is the jump from a log
                    # line to the trace that explains it, which is the move this whole stack
                    # exists to support.
                    "trace_id": labels.get("trace_id"),
                    "exception_type": labels.get("exception_type"),
                    "logger": labels.get("scope_name"),
                }
            )

    entries.sort(key=lambda e: e["timestamp"], reverse=True)
    return entries


async def _count(query: str, minutes: int) -> int:
    """Counts matching lines with a LogQL metric query.

    Counting by fetching every line and calling len() reads naturally and does not work: a
    moderately chatty service over a few hours is tens of thousands of lines, and Loki times out
    streaming them back to be discarded. `count_over_time` is aggregated inside Loki and returns
    one number.
    """
    config = settings()

    try:
        async with httpx.AsyncClient(timeout=config.http_timeout_seconds) as client:
            response = await client.get(
                f"{config.loki_url}/loki/api/v1/query",
                params={"query": f"sum(count_over_time({query}[{minutes}m]))"},
            )
    except httpx.HTTPError as exc:
        raise LokiError(f"Could not reach Loki at {config.loki_url}: {exc}") from exc

    if response.status_code >= 400:
        raise LokiError(f"Loki rejected the query ({response.status_code}): {response.text[:300]}")

    results = response.json().get("data", {}).get("result", [])

    # An empty result means no matching lines, which is a zero rather than an error.
    if not results:
        return 0

    return int(float(results[0]["value"][1]))


def _iso(timestamp_ns: int) -> str:
    seconds, remainder = divmod(timestamp_ns, 1_000_000_000)
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
        + f".{remainder // 1_000_000:03d}Z"
    )


def _selector(service: str, level: str | None = None) -> str:
    selector = f'{{{_SERVICE_LABEL}="{service}"}}'

    if level:
        # severity_text is structured metadata, not a stream label, so it filters with `|`
        # rather than inside the selector.
        selector += f" | severity_text = `{level.capitalize()}`"

    return selector


# --------------------------------------------------------------------------- tools ----


@read_only(
    server,
    "Recent log lines for one service, newest first. Use `level` to narrow to Error or "
    "Warning. Start here when you know which service is misbehaving and want to see what it "
    "said.",
)
async def get_service_logs(
    service: str,
    minutes: int = 15,
    level: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Args:
    service: Logical service name, e.g. `orders`.
    minutes: How far back to look.
    level: Optional severity filter: Error, Warning, Information.
    limit: Maximum lines to return.
    """
    config = settings()
    entries = await _query_range(_selector(service, level), minutes, limit)
    window = window_label(minutes)

    if not entries:
        return empty(
            window,
            f"{service} logs" + (f" at level {level}" if level else ""),
            service=service,
        )

    shown, was_truncated = truncated(entries, min(limit, config.max_results))

    return result(
        found=len(entries),
        window=window,
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        service=service,
    )


@read_only(
    server,
    "Full-text search across log lines. Scope it with `service` and a tight window: Loki "
    "indexes labels rather than content, so a wide unfiltered search is slow.",
)
async def search_logs(
    query: str,
    service: str | None = None,
    minutes: int = 15,
    limit: int = 50,
) -> dict[str, Any]:
    """Args:
    query: Substring to look for, e.g. `deadlock` or `BrokenCircuitException`.
    service: Restrict to one service. Strongly recommended.
    minutes: How far back to look.
    limit: Maximum lines to return.
    """
    selector = (
        _selector(service) if service else f'{{{_SERVICE_LABEL}=~".+"}}'
    )
    # Loki's |= is a literal substring match, so a query containing backticks would break the
    # expression rather than search for them.
    safe = query.replace("`", "")
    entries = await _query_range(f"{selector} |= `{safe}`", minutes, limit)
    window = window_label(minutes)

    if not entries:
        return empty(window, f"logs containing '{query}'", query=query, service=service)

    shown, was_truncated = truncated(entries, min(limit, settings().max_results))

    return result(
        found=len(entries),
        window=window,
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        query=query,
        service=service,
    )


@read_only(
    server,
    "Error and Critical lines for one service. An empty result is meaningful: several failure "
    "modes — slow queries, N+1 queries, retry storms — produce no error log at all, and rule "
    "those in rather than out.",
)
async def get_recent_errors(service: str, minutes: int = 15, limit: int = 25) -> dict[str, Any]:
    """Args:
    service: Logical service name.
    minutes: How far back to look.
    limit: Maximum lines to return.
    """
    entries = await _query_range(
        f'{{{_SERVICE_LABEL}="{service}"}} | severity_text =~ `Error|Critical`', minutes, limit
    )
    window = window_label(minutes)

    if not entries:
        return empty(
            window,
            f"{service} logs at Error or Critical",
            service=service,
            interpretation=(
                "No errors logged. If the service is measurably unhealthy, the fault is one "
                "that fails silently — look at latency, traces and the database instead."
            ),
        )

    shown, was_truncated = truncated(entries, min(limit, settings().max_results))

    return result(
        found=len(entries),
        window=window,
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        service=service,
    )


@read_only(
    server,
    "Exception types for one service, counted and ranked. The fastest way to tell one loud "
    "exception apart from several unrelated ones, and to see whether errors are one kind or "
    "many.",
)
async def get_exception_statistics(service: str, minutes: int = 30) -> dict[str, Any]:
    """Args:
    service: Logical service name.
    minutes: How far back to look.
    """
    window = window_label(minutes)

    # Counted in Loki, so the totals are exact regardless of how many lines there are.
    total = await _count(
        f'{{{_SERVICE_LABEL}="{service}"}} | severity_text =~ `Error|Critical`', minutes
    )

    if total == 0:
        return empty(window, f"{service} exceptions", service=service)

    # Sampled for the type breakdown and the examples, because those need the lines themselves.
    entries = await _query_range(
        f'{{{_SERVICE_LABEL}="{service}"}} | severity_text =~ `Error|Critical`',
        minutes,
        _SAMPLE_LIMIT,
    )

    if not entries:
        return empty(window, f"{service} exceptions", service=service)

    counts: Counter[str] = Counter()
    examples: dict[str, str] = {}

    for entry in entries:
        # The structured attribute where present; otherwise the type is recovered from the
        # message, because a stack trace logged as text still names its exception.
        kind = entry.get("exception_type") or _exception_from_text(entry["message"])
        counts[kind] += 1
        examples.setdefault(kind, entry["message"][:300])

    items = [
        {
            "exception_type": kind,
            "count": count,
            "share": round(count / len(entries), 3),
            "example": examples[kind],
        }
        for kind, count in counts.most_common(settings().max_results)
    ]

    return result(
        found=total,
        window=window,
        items=items,
        service=service,
        distinct_types=len(counts),
        sampled=len(entries),
        note=(
            None
            if len(entries) >= total
            else (
                f"Counts are over a sample of {len(entries)} of {total} error lines, so shares "
                "are approximate. The total is exact."
            )
        ),
    )


_EXCEPTION_PATTERN = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Exception|Error))\b")


def _exception_from_text(message: str) -> str:
    match = _EXCEPTION_PATTERN.search(message)
    return match.group(1) if match else "Unclassified"


@read_only(
    server,
    "Share of a service's log lines that are errors, over a window. A log-derived figure: it "
    "answers 'how much of what this service said was an error', not 'how many requests "
    "failed'. For the request-level figure use metrics-mcp's get_error_rate.",
)
async def get_error_rate(service: str, minutes: int = 15) -> dict[str, Any]:
    """Args:
    service: Logical service name.
    minutes: How far back to look.
    """
    window = window_label(minutes)

    total = await _count(_selector(service), minutes)
    errors = await _count(
        f'{{{_SERVICE_LABEL}="{service}"}} | severity_text =~ `Error|Critical`', minutes
    )

    if total == 0:
        return {
            "service": service,
            "window": window,
            "error_rate": None,
            "total_lines": 0,
            "error_lines": 0,
            "note": (
                "The service logged nothing in this window, so no rate can be computed. That "
                "is itself worth checking: a service under load normally logs something."
            ),
        }

    return {
        "service": service,
        "window": window,
        "total_lines": total,
        "error_lines": errors,
        "error_rate": round(errors / total, 4),
        "note": (
            "Share of log lines, not of requests. A service that logs verbosely will show a "
            "low rate while failing every request."
        ),
    }


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
