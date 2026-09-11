"""Intent -> the plan it implies, in one place because three things now read it.

The rule router turns a keyword match into this; the Phase 8 dataset labels every training
example from it; and the routing benchmark scores a model's tool list against it. A copy in any
of those three would be a copy that could disagree with the planner the agent actually runs.

The tools are the ones the scenario table in `sample-services/chaos/scenarios.md` lists under
"Expected tools" for failures of that shape, which is what makes this table checkable against
something rather than invented.
"""

from __future__ import annotations

from routing.schema import Intent, RouteDecision

INTENT_PLANS: dict[Intent, tuple[bool, bool, tuple[str, ...]]] = {
    Intent.FULL_INVESTIGATION: (
        True,
        True,
        (
            "logs-mcp/get_recent_errors",
            "metrics-mcp/get_error_rate",
            "metrics-mcp/get_response_time",
            "traces-mcp/get_failed_traces",
            "git-mcp/get_recent_commits",
        ),
    ),
    Intent.ERROR_ANALYSIS: (
        True,
        True,
        ("logs-mcp/get_recent_errors", "logs-mcp/get_exception_statistics"),
    ),
    Intent.LOG_QUERY: (False, True, ("logs-mcp/get_service_logs", "logs-mcp/search_logs")),
    Intent.METRIC_QUERY: (False, True, ("metrics-mcp/get_service_metrics",)),
    Intent.TRACE_QUERY: (False, True, ("traces-mcp/get_recent_traces",)),
    Intent.PERFORMANCE_ANALYSIS: (
        True,
        True,
        ("metrics-mcp/get_response_time", "traces-mcp/get_slowest_spans"),
    ),
    Intent.DATABASE_HEALTH: (
        True,
        True,
        (
            "database-mcp/get_connection_count",
            "database-mcp/get_slow_queries",
            "database-mcp/get_locks_and_deadlocks",
        ),
    ),
    Intent.DEPLOYMENT_CHECK: (False, True, ("git-mcp/get_recent_commits",)),
    Intent.CODE_LOOKUP: (True, True, ("source-code-mcp/search_code", "source-code-mcp/read_file")),
    Intent.CONFIG_LOOKUP: (True, True, ("source-code-mcp/search_code",)),
    Intent.SERVICE_TOPOLOGY: (True, True, ("traces-mcp/get_service_dependencies",)),
    # No live signal: both are answered from the knowledge base alone.
    Intent.HISTORICAL_SIMILARITY: (True, False, ()),
    Intent.KNOWLEDGE_QUESTION: (True, False, ()),
    Intent.REMEDIATION_QUESTION: (True, False, ()),
    # Unknown shape, so collect broadly and let the evidence decide.
    Intent.GENERAL_QUESTION: (
        True,
        True,
        ("logs-mcp/get_recent_errors", "metrics-mcp/get_service_metrics"),
    ),
}


def decide(intent: Intent, target_service: str | None = None) -> RouteDecision:
    """The decision this intent implies, whoever recognised it.

    A router's job is the intent and the service; everything else follows from the table above.
    That is deliberate and it is what the Phase 8 benchmark measures against: a tuned model that
    emits a different tool list for LOG_QUERY has not learned something extra, it has drifted
    from the planner that will act on its answer.
    """
    requires_rag, requires_mcp, tools = INTENT_PLANS[intent]

    return RouteDecision(
        intent=intent,
        requires_rag=requires_rag,
        requires_mcp=requires_mcp,
        tools=list(tools),
        target_service=target_service,
    )
