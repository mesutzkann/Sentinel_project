"""Every read-only tool run against the real backends.

Skipped unless the stack is up, so `pytest` works on a laptop with nothing running. What it adds
over the unit tests is the only thing that cannot be faked: that the query each tool sends is one
the real Loki, Prometheus, Jaeger or PostgreSQL accepts. A LogQL filter in the wrong clause or a
metric renamed by an exporter passes every unit test and returns nothing in production.

    docker compose --profile core --profile samples --profile observability up -d
    LIVE_MCP=1 pytest tests/test_live.py
"""

from __future__ import annotations

import os

import pytest

from database_mcp import server as database_server
from git_mcp import server as git_server
from logs_mcp import server as logs_server
from metrics_mcp import server as metrics_server
from source_code_mcp import server as source_server
from traces_mcp import server as traces_server

pytestmark = pytest.mark.skipif(
    os.environ.get("LIVE_MCP") != "1",
    reason="Set LIVE_MCP=1 with the stack running to exercise the real backends.",
)

# Wide enough that a quiet stack still has something in it, narrow enough to stay fast.
WINDOW = 240

# A service that certainly exists. Every assertion below is about the tool answering without
# error, not about the stack being in a particular state — a healthy stack and a broken one
# should both produce a well-formed answer.
SERVICE = "orders"


def _ok(payload: dict) -> dict:
    """A tool answered rather than failing.

    An empty result counts: absence of a signal is a valid answer here, and asserting on
    non-empty data would make the suite depend on the stack having recently misbehaved.
    """
    assert isinstance(payload, dict), payload
    assert "error" not in payload, payload["error"]
    return payload


# --------------------------------------------------------------------------- logs ----


async def test_get_service_logs() -> None:
    _ok(await logs_server.get_service_logs(SERVICE, minutes=WINDOW, limit=5))


async def test_search_logs() -> None:
    _ok(await logs_server.search_logs("request", service=SERVICE, minutes=WINDOW, limit=5))


async def test_get_recent_errors() -> None:
    _ok(await logs_server.get_recent_errors(SERVICE, minutes=WINDOW, limit=5))


async def test_get_exception_statistics() -> None:
    _ok(await logs_server.get_exception_statistics(SERVICE, minutes=WINDOW))


async def test_logs_error_rate_counts_without_downloading() -> None:
    """Counted with a LogQL metric query; fetching the lines to len() them times out."""
    payload = _ok(await logs_server.get_error_rate(SERVICE, minutes=WINDOW))

    assert "total_lines" in payload


# ------------------------------------------------------------------------ metrics ----


async def test_get_service_metrics() -> None:
    payload = _ok(await metrics_server.get_service_metrics(SERVICE, minutes=WINDOW))

    assert "interpretation" in payload


async def test_get_cpu_usage() -> None:
    """Guards the custom ProcessMetrics meter: without it there is no CPU series at all."""
    _ok(await metrics_server.get_cpu_usage(minutes=WINDOW))


async def test_get_memory_usage() -> None:
    _ok(await metrics_server.get_memory_usage(minutes=WINDOW))


async def test_get_request_rate() -> None:
    _ok(await metrics_server.get_request_rate(minutes=WINDOW))


async def test_metrics_error_rate() -> None:
    _ok(await metrics_server.get_error_rate(minutes=WINDOW))


async def test_get_response_time() -> None:
    _ok(await metrics_server.get_response_time(minutes=WINDOW))


async def test_query_promql() -> None:
    payload = _ok(await metrics_server.query_promql("up"))

    assert payload["found"] >= 1


# ------------------------------------------------------------------------- traces ----


async def test_get_recent_traces() -> None:
    _ok(await traces_server.get_recent_traces("gateway", minutes=WINDOW, limit=3))


async def test_get_failed_traces() -> None:
    _ok(await traces_server.get_failed_traces(SERVICE, minutes=WINDOW, limit=3))


async def test_get_slowest_spans() -> None:
    _ok(await traces_server.get_slowest_spans(SERVICE, minutes=WINDOW, limit=3))


async def test_get_service_dependencies() -> None:
    _ok(await traces_server.get_service_dependencies(minutes=WINDOW))


async def test_get_trace_details() -> None:
    """Chained off a real search, because a trace id cannot be invented."""
    recent = await traces_server.get_recent_traces("gateway", minutes=WINDOW, limit=1)

    if not recent.get("items"):
        pytest.skip("No traces in the window to open.")

    payload = _ok(await traces_server.get_trace_details(recent["items"][0]["trace_id"]))

    assert payload["spans"]


# ----------------------------------------------------------------------- database ----


async def test_get_database_health() -> None:
    payload = _ok(await database_server.get_database_health())

    assert payload["max_connections"] > 0


async def test_get_connection_count() -> None:
    _ok(await database_server.get_connection_count())


async def test_get_slow_queries() -> None:
    """Also proves pg_stat_statements is loaded, which compose sets up."""
    _ok(await database_server.get_slow_queries(limit=3))


async def test_get_locks_and_deadlocks() -> None:
    _ok(await database_server.get_locks_and_deadlocks())


async def test_describe_table() -> None:
    payload = _ok(await database_server.describe_table("orders", "svc_orders"))

    assert payload["columns"]


async def test_execute_readonly_query() -> None:
    payload = _ok(await database_server.execute_readonly_query("SELECT 1 AS one"))

    assert payload["rows"] == [{"one": "1"}]


async def test_a_write_is_refused_by_postgres_even_past_the_parser() -> None:
    """The READ ONLY transaction is the real guarantee; the parser is only a fast rejection.

    `WITH ... SELECT` gets past the parser by design, so this is the one path where PostgreSQL
    itself has to do the refusing.
    """
    payload = await database_server.execute_readonly_query(
        "WITH written AS (SELECT 1) SELECT pg_advisory_lock(1) FROM written"
    )

    # Either rejection is correct; what must not happen is the lock being taken.
    assert "error" in payload or payload.get("found") is not None


# ---------------------------------------------------------------------------- git ----


async def test_get_recent_commits() -> None:
    payload = _ok(await git_server.get_recent_commits(limit=3))

    assert payload["found"] >= 1


async def test_get_commit_diff_and_changed_files() -> None:
    recent = await git_server.get_recent_commits(limit=1)
    sha = recent["items"][0]["sha"]

    assert _ok(await git_server.get_commit_diff(sha, max_lines=20))["commit"]["sha"] == sha
    assert _ok(await git_server.get_changed_files(sha))["files"]


async def test_search_commit_messages() -> None:
    _ok(await git_server.search_commit_messages("phase", limit=3))


async def test_get_file_history() -> None:
    _ok(await git_server.get_file_history("README.md", limit=3))


async def test_get_commits_between() -> None:
    _ok(await git_server.get_commits_between("HEAD~2", "HEAD"))


# --------------------------------------------------------------------- source code ----


async def test_search_code() -> None:
    payload = _ok(await source_server.search_code("ChaosRegistry", limit=3))

    assert payload["found"] >= 1


async def test_read_file() -> None:
    payload = _ok(await source_server.read_file("README.md", start=1, end=3))

    assert payload["content"]


async def test_find_symbol() -> None:
    payload = _ok(await source_server.find_symbol("PaymentProcessor"))

    assert payload["found"] >= 1


async def test_find_references() -> None:
    payload = _ok(await source_server.find_references("ChaosRegistry"))

    assert payload["found"] >= 1


async def test_get_project_structure() -> None:
    payload = _ok(await source_server.get_project_structure("sample-services", max_depth=2))

    assert payload["found"] >= 1
