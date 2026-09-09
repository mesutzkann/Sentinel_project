"""The collectors, against recorded tool output rather than a running stack.

The dictionaries below are real: they were taken from the MCP servers running against the sample
estate. That matters more than it looks — a summariser written against a guessed shape reads
fields that are not there and silently produces "0 in the window" for every result, which is
indistinguishable from a healthy system.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.context import EvidenceSource, InvestigationContext, ToolBudgetExhaustedError
from agents.nodes.collect import CollectorNode, ToolRequest
from agents.nodes.collectors import (
    WEIGHT_GAUGE,
    WEIGHT_NEGATIVE,
    WEIGHT_POSITIVE,
    WEIGHT_SATURATED,
    CheckDeploymentsNode,
    CollectDatabaseNode,
    CollectLogsNode,
    CollectMetricsNode,
    CollectTracesNode,
    InspectCodeNode,
)
from agents.state_machine import EventType
from agents.states import State
from mcp_client.client import ToolCallResult

# ---- recorded output ------------------------------------------------------------------

NO_ERRORS = {
    "found": 0,
    "window": "last 30m",
    "items": [],
    "note": "Nothing matched. Searched orders logs at Error or Critical.",
    "service": "orders",
    "interpretation": (
        "No errors logged. If the service is measurably unhealthy, the fault is one that fails "
        "silently — look at latency, traces and the database instead."
    ),
}

SOME_ERRORS = {
    "found": 347,
    "window": "last 30m",
    "items": [{"message": "Npgsql.NpgsqlException: The connection pool has been exhausted"}],
    "service": "orders",
}

RESPONSE_TIME = {
    "window": "last 30m",
    "unit": "seconds",
    "services": {"orders": {"p50": 0.0025, "p95": 0.0047, "p99": 0.005}},
}

ERROR_RATE = {"window": "last 15m", "services": {"orders": 0.0}}

CONNECTIONS_HEALTHY = {
    "max_connections": 300,
    "used": 6,
    "headroom": 294,
    "by_client": [],
    "note": "These are server-side connections.",
}

CONNECTIONS_SATURATED = {
    "max_connections": 300,
    "used": 300,
    "headroom": 0,
    "by_client": [],
    "note": "These are server-side connections.",
}

NO_LOCKS = {
    "deadlocks_since_reset": 0,
    "stats_reset": None,
    "current_lock_waits": [],
    "interpretation": "No deadlocks recorded and nothing is currently blocked.",
}

DEADLOCKS = {
    "deadlocks_since_reset": 12,
    "stats_reset": None,
    "current_lock_waits": [{"blocked": 1834, "blocking": 1902}],
    "interpretation": "Deadlocks have been recorded.",
}

COMMITS = {
    "found": 2,
    "window": "last 30m",
    "items": [
        {"short_sha": "cef3434e", "subject": "drop MaxPoolSize to 20", "files_changed": 1},
        {"short_sha": "f9ad5d15", "subject": "bump timeout", "files_changed": 2},
    ],
}

DEPENDENCIES = {
    "window": "last 1h",
    "found": 4,
    "edges": [{"parent": "gateway", "child": "orders"}],
    "source": "derived from traces",
    "note": "Rebuilt by walking recent traces.",
}

CODE_MATCHES = {
    "found": 12,
    "window": "the repository",
    "items": [
        {"path": "sample-services/orders/appsettings.json", "line": 12, "match": "MaxPoolSize=20"},
        {"path": "sample-services/orders/Program.cs", "line": 44, "match": "MaxPoolSize"},
    ],
    "note": "",
    "query": "MaxPoolSize",
}


# ---- doubles --------------------------------------------------------------------------


class _FakeClient:
    """Answers from a table keyed by qualified tool name."""

    def __init__(self, responses: dict[str, Any], failures: set[str] | None = None) -> None:
        self._responses = responses
        self._failures = failures or set()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        qualified_name: str,
        arguments: dict[str, Any] | None = None,
        approval_token: str | None = None,
    ) -> ToolCallResult:
        self.calls.append((qualified_name, arguments or {}))
        server, _, tool = qualified_name.partition("/")

        if qualified_name in self._failures:
            return ToolCallResult(
                tool=tool,
                server=server,
                arguments=arguments or {},
                success=False,
                latency_ms=5,
                error="server unreachable",
            )

        return ToolCallResult(
            tool=tool,
            server=server,
            arguments=arguments or {},
            success=True,
            latency_ms=42,
            content=self._responses.get(qualified_name),
        )


def _context(**kwargs: Any) -> InvestigationContext:
    defaults: dict[str, Any] = {
        "investigation_id": "11111111-1111-1111-1111-111111111111",
        "incident_code": "INC-00142",
        "query": "why is orders failing",
        "service_hint": "orders",
    }

    return InvestigationContext(**{**defaults, **kwargs})


# ---- the shared behaviour --------------------------------------------------------------


async def test_a_failed_tool_becomes_a_note_and_never_evidence() -> None:
    """"Loki did not answer" is a fact about the investigation, not about the incident.

    Recording it as evidence would let a hypothesis be built on the health of the observability
    stack while an incident is open.
    """
    client = _FakeClient(
        {"logs-mcp/get_exception_statistics": SOME_ERRORS},
        failures={"logs-mcp/get_recent_errors"},
    )
    node = CollectLogsNode(client)  # type: ignore[arg-type]
    ctx = _context()

    transition = await node.run(ctx)

    assert len(ctx.evidence) == 1
    assert any("get_recent_errors failed" in note for note in ctx.notes)
    assert (transition.payload or {})["failures"] == ["get_recent_errors"]


async def test_budget_is_spent_for_the_whole_collector_or_not_at_all() -> None:
    """Halfway through is a signal nothing downstream can interpret."""
    client = _FakeClient({})
    node = CollectDatabaseNode(client)  # type: ignore[arg-type]
    ctx = _context(tool_budget=2)  # the database collector wants three

    with pytest.raises(ToolBudgetExhaustedError):
        await node.run(ctx)

    assert client.calls == []
    assert ctx.tool_calls_made == 0


async def test_finding_nothing_is_recorded_as_a_result() -> None:
    """It is what separates a slow query with no errors from a pool exhaustion with many."""
    client = _FakeClient(
        {
            "logs-mcp/get_recent_errors": NO_ERRORS,
            "logs-mcp/get_exception_statistics": NO_ERRORS,
        }
    )
    ctx = _context()

    await CollectLogsNode(client).run(ctx)  # type: ignore[arg-type]

    assert len(ctx.evidence) == 2
    assert all(item.weight == WEIGHT_NEGATIVE for item in ctx.evidence)
    assert "0 in last 30m" in ctx.evidence[0].summary
    # The server's own reading of an empty result is carried through.
    assert "fails silently" in ctx.evidence[0].summary


async def test_a_collector_goes_to_the_next_state_on_the_plan() -> None:
    """PLAN decides the order. A collector that hard-coded its successor would make it a lie."""
    client = _FakeClient({"metrics-mcp/get_error_rate": ERROR_RATE})
    ctx = _context()
    ctx.plan = [State.COLLECT_TRACES, State.SEARCH_HISTORY]

    transition = await CollectMetricsNode(client).run(ctx)  # type: ignore[arg-type]

    assert transition.next_state is State.COLLECT_TRACES
    assert ctx.plan == [State.SEARCH_HISTORY]


async def test_an_empty_plan_leads_to_reasoning() -> None:
    client = _FakeClient({"metrics-mcp/get_error_rate": ERROR_RATE})
    ctx = _context()

    transition = await CollectMetricsNode(client).run(ctx)  # type: ignore[arg-type]

    assert transition.next_state is State.GENERATE_HYPOTHESES


async def test_every_fact_is_announced_as_an_event() -> None:
    client = _FakeClient(
        {
            "logs-mcp/get_recent_errors": SOME_ERRORS,
            "logs-mcp/get_exception_statistics": SOME_ERRORS,
        }
    )
    ctx = _context()

    transition = await CollectLogsNode(client).run(ctx)  # type: ignore[arg-type]

    assert len(transition.events) == 2
    assert all(e.type is EventType.EVIDENCE_FOUND for e in transition.events)
    assert transition.events[0].payload is not None
    assert transition.events[0].payload["index"] == 0
    assert transition.events[0].payload["source"] is EvidenceSource.LOGS


# ---- what each collector decides -------------------------------------------------------


async def test_logs_need_a_service_and_say_so_when_they_have_none() -> None:
    """A wrong guess at the service is worse than a step that admits it had none."""
    client = _FakeClient({})
    ctx = _context(service_hint=None, query="everything is broken")

    transition = await CollectLogsNode(client).run(ctx)  # type: ignore[arg-type]

    assert client.calls == []
    assert ctx.tool_calls_made == 0
    assert (transition.payload or {})["skipped"] is True
    assert any("no single service" in note for note in ctx.notes)


async def test_metrics_work_without_a_service() -> None:
    """A cascade is what it looks like from outside any one service."""
    client = _FakeClient(
        {"metrics-mcp/get_error_rate": ERROR_RATE, "metrics-mcp/get_response_time": RESPONSE_TIME}
    )
    ctx = _context(service_hint=None)

    await CollectMetricsNode(client).run(ctx)  # type: ignore[arg-type]

    assert len(client.calls) == 2
    assert "service" not in client.calls[0][1]


async def test_response_time_percentiles_reach_the_summary() -> None:
    """The reasoning node needs the numbers; a collector that said "latency is fine" would have
    made the diagnosis where nobody can see it."""
    client = _FakeClient({"metrics-mcp/get_response_time": RESPONSE_TIME})
    ctx = _context()

    await CollectMetricsNode(client).run(ctx)  # type: ignore[arg-type]

    summary = next(item.summary for item in ctx.evidence if "response time" in item.summary)

    assert "p99=0.005" in summary
    assert "seconds" in summary
    assert ctx.evidence[0].weight == WEIGHT_GAUGE


async def test_traces_ask_for_the_call_graph_when_no_service_is_named() -> None:
    """Not a fallback: a question with no single service is usually about how they relate."""
    client = _FakeClient({"traces-mcp/get_service_dependencies": DEPENDENCIES})
    ctx = _context(service_hint=None)

    await CollectTracesNode(client).run(ctx)  # type: ignore[arg-type]

    assert [name for name, _ in client.calls] == ["traces-mcp/get_service_dependencies"]
    assert "dependency edges: 4" in ctx.evidence[0].summary


async def test_saturated_connections_weigh_more_than_a_healthy_gauge() -> None:
    """Saturation is measured, not judged — the server returns the headroom."""
    healthy = _FakeClient({"database-mcp/get_connection_count": CONNECTIONS_HEALTHY})
    saturated = _FakeClient({"database-mcp/get_connection_count": CONNECTIONS_SATURATED})

    healthy_ctx, saturated_ctx = _context(), _context()
    await CollectDatabaseNode(healthy).run(healthy_ctx)  # type: ignore[arg-type]
    await CollectDatabaseNode(saturated).run(saturated_ctx)  # type: ignore[arg-type]

    assert healthy_ctx.evidence[0].weight == WEIGHT_GAUGE
    assert saturated_ctx.evidence[0].weight == WEIGHT_SATURATED
    assert "300 of 300 used, 0 spare" in saturated_ctx.evidence[0].summary


async def test_deadlocks_present_weigh_more_than_deadlocks_absent() -> None:
    present = _FakeClient({"database-mcp/get_locks_and_deadlocks": DEADLOCKS})
    absent = _FakeClient({"database-mcp/get_locks_and_deadlocks": NO_LOCKS})

    present_ctx, absent_ctx = _context(), _context()
    await CollectDatabaseNode(present).run(present_ctx)  # type: ignore[arg-type]
    await CollectDatabaseNode(absent).run(absent_ctx)  # type: ignore[arg-type]

    assert present_ctx.evidence[0].weight == WEIGHT_POSITIVE
    assert absent_ctx.evidence[0].weight == WEIGHT_NEGATIVE
    assert "deadlocks since reset: 12" in present_ctx.evidence[0].summary


async def test_the_database_collector_needs_no_service() -> None:
    """One PostgreSQL behind the estate; "is the database the problem" is not per-service."""
    client = _FakeClient({"database-mcp/get_connection_count": CONNECTIONS_HEALTHY})
    ctx = _context(service_hint=None)

    await CollectDatabaseNode(client).run(ctx)  # type: ignore[arg-type]

    assert all(args == {} or "service" not in args for _, args in client.calls)


async def test_deployments_use_the_investigation_window_not_a_commit_count() -> None:
    """"The last five commits" cannot exonerate a deploy; "the commits since it started" can."""
    client = _FakeClient({"git-mcp/get_recent_commits": COMMITS})
    ctx = _context(window_minutes=90)

    await CheckDeploymentsNode(client).run(ctx)  # type: ignore[arg-type]

    _, arguments = client.calls[0]

    assert arguments["since_minutes"] == 90
    assert "drop MaxPoolSize to 20" in ctx.evidence[0].summary


async def test_deployments_never_pass_a_repo() -> None:
    """git-mcp's `repo` is a path under the mount root, not a service name, and the estate is one
    repository. Passing the service resolved to a directory that is not there and failed every
    deployment check — safely, as a note, which is how it was found."""
    client = _FakeClient({"git-mcp/get_recent_commits": COMMITS})

    await CheckDeploymentsNode(client).run(_context())  # type: ignore[arg-type]

    _, arguments = client.calls[0]

    assert "repo" not in arguments


async def test_code_search_narrows_to_the_service_and_names_the_files() -> None:
    client = _FakeClient({"source-code-mcp/search_code": CODE_MATCHES})
    ctx = _context(query="where is MaxPoolSize configured")

    await InspectCodeNode(client).run(ctx)  # type: ignore[arg-type]

    _, arguments = client.calls[0]

    assert arguments["path_contains"] == "orders"
    assert arguments["query"] == "where is MaxPoolSize configured"
    assert "appsettings.json" in ctx.evidence[0].summary


async def test_every_collector_declares_a_distinct_source() -> None:
    """Two collectors sharing a source would make the diversity bonus count one twice."""
    client = _FakeClient({})
    nodes: list[CollectorNode] = [
        CollectLogsNode(client),  # type: ignore[arg-type]
        CollectMetricsNode(client),  # type: ignore[arg-type]
        CollectTracesNode(client),  # type: ignore[arg-type]
        CollectDatabaseNode(client),  # type: ignore[arg-type]
        CheckDeploymentsNode(client),  # type: ignore[arg-type]
        InspectCodeNode(client),  # type: ignore[arg-type]
    ]

    sources = [node.source for node in nodes]

    assert len(set(sources)) == len(sources)


async def test_every_collector_calls_only_qualified_read_only_tools() -> None:
    """A collector reaching for a destructive tool would be refused by policy at runtime; this
    catches it at the point where the argument table is written instead."""
    client = _FakeClient({})
    ctx = _context()
    nodes: list[CollectorNode] = [
        CollectLogsNode(client),  # type: ignore[arg-type]
        CollectMetricsNode(client),  # type: ignore[arg-type]
        CollectTracesNode(client),  # type: ignore[arg-type]
        CollectDatabaseNode(client),  # type: ignore[arg-type]
        CheckDeploymentsNode(client),  # type: ignore[arg-type]
        InspectCodeNode(client),  # type: ignore[arg-type]
    ]
    destructive = {"execute_write_query", "revert_commit", "apply_patch", "restart_container"}

    for node in nodes:
        for request in node.plan_calls(ctx):
            assert request.qualified_name.count("/") == 1
            assert request.tool not in destructive


def test_the_request_type_splits_the_tool_off_its_server() -> None:
    assert ToolRequest("logs-mcp/get_recent_errors", {}).tool == "get_recent_errors"
