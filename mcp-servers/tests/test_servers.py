"""Every tool on every server, checked without a running backend.

Two things are checked for all thirty-four tools, because both are contracts the AI service
depends on and neither is visible when reading one server in isolation:

* Registration and annotation. A tool that forgets `read_only_hint` is treated as destructive by
  the policy layer and silently stops being callable.
* An LLM-usable description. The description is the only thing the model has when choosing
  between `logs-mcp/get_error_rate` and `metrics-mcp/get_error_rate`.

The behavioural tests then cover the parts that are logic rather than a query: guards, path
resolution, and result shaping. Queries against live Loki, Prometheus, Jaeger and PostgreSQL are
exercised by the integration check in tests/test_live.py, which skips when they are not running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from _shared.results import empty, result, truncated, window_label
from database_mcp import server as database_server
from git_mcp import server as git_server
from logs_mcp import server as logs_server
from metrics_mcp import server as metrics_server
from source_code_mcp import server as source_server
from traces_mcp import server as traces_server

ALL_SERVERS = {
    "logs-mcp": logs_server.server,
    "metrics-mcp": metrics_server.server,
    "traces-mcp": traces_server.server,
    "database-mcp": database_server.server,
    "git-mcp": git_server.server,
    "source-code-mcp": source_server.server,
}

# The tool list from docs/planning.md section 5, restricted to the read-only ones Phase 4 owns.
EXPECTED_TOOLS = {
    "logs-mcp": {
        "get_service_logs",
        "search_logs",
        "get_recent_errors",
        "get_exception_statistics",
        "get_error_rate",
    },
    "metrics-mcp": {
        "get_service_metrics",
        "get_cpu_usage",
        "get_memory_usage",
        "get_request_rate",
        "get_error_rate",
        "get_response_time",
        "query_promql",
    },
    "traces-mcp": {
        "get_recent_traces",
        "get_failed_traces",
        "get_trace_details",
        "get_slowest_spans",
        "get_service_dependencies",
    },
    "database-mcp": {
        "get_database_health",
        "get_connection_count",
        "get_slow_queries",
        "get_locks_and_deadlocks",
        "describe_table",
        "execute_readonly_query",
    },
    "git-mcp": {
        "get_recent_commits",
        "get_commit_diff",
        "get_changed_files",
        "search_commit_messages",
        "get_file_history",
        "get_commits_between",
    },
    "source-code-mcp": {
        "search_code",
        "read_file",
        "find_symbol",
        "find_references",
        "get_project_structure",
    },
}


def _tools(server) -> dict:
    return {t.name: t for t in asyncio.run(server.list_tools())}


# ------------------------------------------------------------------- registration ----


@pytest.mark.parametrize("name", sorted(ALL_SERVERS))
def test_server_registers_exactly_the_planned_tools(name: str) -> None:
    assert set(_tools(ALL_SERVERS[name])) == EXPECTED_TOOLS[name]


def test_thirty_four_tools_in_total() -> None:
    """The number the AI service's registry should discover."""
    assert sum(len(t) for t in EXPECTED_TOOLS.values()) == 34


@pytest.mark.parametrize("name", sorted(ALL_SERVERS))
def test_every_tool_is_annotated_read_only(name: str) -> None:
    """Phase 4 is read-only throughout.

    This is the annotation the policy layer reads. A tool that loses it becomes uncallable
    without an approval token that does not exist yet, which would look like a broken agent
    rather than a broken annotation.
    """
    for tool_name, tool in _tools(ALL_SERVERS[name]).items():
        assert tool.annotations is not None, f"{tool_name} has no annotations"
        assert tool.annotations.read_only_hint is True, f"{tool_name} is not marked read-only"
        assert tool.annotations.destructive_hint is False, f"{tool_name} is marked destructive"


@pytest.mark.parametrize("name", sorted(ALL_SERVERS))
def test_every_tool_has_a_usable_description(name: str) -> None:
    """The description is what the model chooses on, so it has to say more than the name."""
    for tool_name, tool in _tools(ALL_SERVERS[name]).items():
        assert tool.description, f"{tool_name} has no description"
        assert len(tool.description) > 40, f"{tool_name} has too thin a description to choose on"


@pytest.mark.parametrize("name", sorted(ALL_SERVERS))
def test_every_tool_publishes_an_input_schema(name: str) -> None:
    for tool_name, tool in _tools(ALL_SERVERS[name]).items():
        assert tool.input_schema.get("type") == "object", f"{tool_name} has no object schema"


def test_server_instructions_explain_what_the_server_is_for() -> None:
    for name, server in ALL_SERVERS.items():
        assert server.instructions and len(server.instructions) > 80, name


# ------------------------------------------------------------------------ results ----


def test_empty_result_says_it_is_a_result() -> None:
    """An empty answer is evidence in this system, and has to read as one."""
    payload = empty("last 15m", "orders logs at Error")

    assert payload["found"] == 0
    assert payload["items"] == []
    assert "not a failure" in payload["note"]


def test_truncation_is_declared() -> None:
    payload = result(found=500, window="last 15m", items=[1, 2, 3], truncated_at=3)

    assert payload["truncated"] is True
    assert "500" in payload["note"]


def test_truncated_reports_whether_it_cut() -> None:
    assert truncated([1, 2, 3], 5) == ([1, 2, 3], False)
    assert truncated([1, 2, 3], 2) == ([1, 2], True)


@pytest.mark.parametrize(
    ("minutes", "label"),
    [(15, "last 15m"), (60, "last 1h"), (120, "last 2h"), (90, "last 1.5h")],
)
def test_window_labels_read_naturally(minutes: int, label: str) -> None:
    assert window_label(minutes) == label


# ------------------------------------------------------------------------- guards ----


@pytest.mark.parametrize(
    "sql",
    [
        "delete from svc_orders.orders",
        "update svc_orders.orders set x = 1",
        "insert into svc_orders.orders default values",
        "drop table svc_orders.orders",
        "truncate svc_orders.orders",
        "select 1; drop table svc_orders.orders",
        "grant all on svc_orders.orders to public",
    ],
)
async def test_readonly_query_refuses_writes(sql: str) -> None:
    """Refused before a connection is opened, so this needs no database."""
    outcome = await database_server.execute_readonly_query(sql)

    assert "error" in outcome, f"{sql!r} was not refused"


@pytest.mark.parametrize(
    "table",
    ["orders; drop table x", "orders'", "../etc", "orders orders"],
)
async def test_describe_table_refuses_non_identifiers(table: str) -> None:
    """Identifiers cannot be parameterised in SQL, so they are validated instead."""
    outcome = await database_server.describe_table(table)

    assert outcome["error"] == "Table and schema must be plain identifiers."


@pytest.mark.parametrize(
    "expr", ["drop series", "tsdb snapshot", "clean_tombstones", "ADMIN something"]
)
async def test_promql_refuses_administrative_expressions(expr: str) -> None:
    outcome = await metrics_server.query_promql(expr)

    assert "error" in outcome


async def test_promql_refuses_an_oversized_expression() -> None:
    outcome = await metrics_server.query_promql("up" * 600)

    assert "longer than 1000" in outcome["error"]


# --------------------------------------------------------------------- source code ----


@pytest.mark.parametrize(
    "path",
    [
        "../../../etc/passwd",
        "../../Windows/System32/drivers/etc/hosts",
        "sample-services/../../outside.txt",
    ],
)
async def test_read_file_refuses_paths_outside_the_repository(path: str) -> None:
    """The arguments here are written by a language model, so this is a live concern."""
    outcome = await source_server.read_file(path)

    assert "resolves outside the repository" in outcome.get("error", "")


async def test_find_symbol_refuses_a_non_identifier() -> None:
    outcome = await source_server.find_symbol("Payment Processor; rm -rf /")

    assert outcome["error"] == "A symbol name must be a plain identifier."


async def test_search_code_reports_an_invalid_regex_rather_than_raising() -> None:
    outcome = await source_server.search_code("(unclosed")

    assert "not a valid regular expression" in outcome["error"]


def test_walk_skips_dependency_directories(tmp_path: Path) -> None:
    """Pruning during the walk, not filtering after it.

    Filtering afterwards costs tens of seconds on a repository with virtualenvs in it — slow
    enough that the tool stops being usable mid-investigation.
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = 1", encoding="utf-8")

    for ignored in ("node_modules", ".venv", "obj"):
        (tmp_path / ignored).mkdir()
        (tmp_path / ignored / "noise.py").write_text("x = 2", encoding="utf-8")

    found = {p.name for p in source_server._walk(tmp_path)}

    assert found == {"real.py"}


# ---------------------------------------------------------------------------- git ----


async def test_git_refuses_a_repo_path_outside_the_mount() -> None:
    with pytest.raises(git_server.RepositoryError, match="outside"):
        git_server._repo("../../..")


# --------------------------------------------------------------------------- logs ----


def test_log_selector_filters_by_severity_outside_the_stream_selector() -> None:
    """severity_text is structured metadata in Loki, so it cannot go inside the braces."""
    selector = logs_server._selector("orders", "error")

    assert selector.startswith('{service_name="orders"}')
    assert "| severity_text = `Error`" in selector


def test_exception_type_is_recovered_from_message_text() -> None:
    """Not every error line carries the type as an attribute; a stack trace still names it."""
    assert (
        logs_server._exception_from_text("System.NullReferenceException: Object reference...")
        == "NullReferenceException"
    )
    assert logs_server._exception_from_text("something went wrong") == "Unclassified"


# ------------------------------------------------------------------------- traces ----


@pytest.mark.parametrize(
    "tags",
    [
        [{"key": "error", "value": True}],
        [{"key": "error", "value": "true"}],
        [{"key": "otel.status_code", "value": "ERROR"}],
        [{"key": "http.response.status_code", "value": 500}],
    ],
)
def test_failed_spans_are_recognised_however_they_are_marked(tags: list[dict]) -> None:
    """The .NET SDK and Jaeger's own convention disagree; missing one halves the failed traces."""
    assert traces_server._is_error({"tags": tags}) is True


def test_a_healthy_span_is_not_an_error() -> None:
    healthy = {"tags": [{"key": "http.response.status_code", "value": 200}]}

    assert traces_server._is_error(healthy) is False


# ----------------------------------------------------------------------- database ----


def test_statements_are_attributed_to_the_service_that_owns_the_schema() -> None:
    """One schema per service is what makes a slow query blameable on a service."""
    assert database_server._attribute("SELECT * FROM svc_orders.orders") == "orders"
    assert database_server._attribute("SELECT * FROM svc_payments.payments") == "payments"
    assert database_server._attribute("SELECT 1") is None
