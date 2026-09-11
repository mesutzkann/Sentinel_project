"""The context, and the two limits it is responsible for enforcing."""

from __future__ import annotations

import pytest

from agents.context import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TOOL_BUDGET,
    EvidenceItem,
    EvidenceSource,
    Hypothesis,
    InvestigationContext,
    ToolBudgetExhaustedError,
)
from routing.schema import Intent, RouteDecision


def _context(**kwargs: object) -> InvestigationContext:
    defaults: dict[str, object] = {
        "investigation_id": "11111111-1111-1111-1111-111111111111",
        "incident_code": "INC-00142",
        "query": "why is orders failing",
    }

    return InvestigationContext(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_the_budget_is_all_or_nothing() -> None:
    """Two calls left and three asked for spends nothing.

    A collector given a partial allowance would have to choose which of its tools to drop, and
    that choice belongs in the collector rather than in whatever the budget happened to have.
    """
    ctx = _context(tool_budget=3)
    ctx.spend_tool_call(1)

    with pytest.raises(ToolBudgetExhaustedError):
        ctx.spend_tool_call(3)

    assert ctx.tool_calls_made == 1
    assert ctx.budget_remaining == 2


def test_the_budget_can_be_spent_exactly() -> None:
    ctx = _context(tool_budget=2)
    ctx.spend_tool_call(2)

    assert ctx.budget_remaining == 0

    with pytest.raises(ToolBudgetExhaustedError):
        ctx.spend_tool_call()


def test_the_planning_document_numbers_are_the_defaults() -> None:
    ctx = _context()

    assert ctx.tool_budget == DEFAULT_TOOL_BUDGET == 25
    assert ctx.max_iterations == DEFAULT_MAX_ITERATIONS == 3


def test_iterations_run_out() -> None:
    """COLLECT_ADDITIONAL_EVIDENCE can send the machine back; this is what stops it forever."""
    ctx = _context()

    assert [ctx.begin_iteration() for _ in range(4)] == [True, True, True, False]
    assert ctx.iteration == 3


def test_the_route_service_beats_the_incident_hint() -> None:
    """The router read the question; the incident row only knows where the alert fired."""
    ctx = _context(
        service_hint="gateway",
        route=RouteDecision(
            intent=Intent.LOG_QUERY,
            requires_rag=False,
            requires_mcp=True,
            tools=[],
            target_service="payments",
        ),
    )

    assert ctx.target_service == "payments"


def test_the_hint_survives_a_route_that_named_nobody() -> None:
    ctx = _context(
        service_hint="gateway",
        route=RouteDecision(
            intent=Intent.GENERAL_QUESTION,
            requires_rag=True,
            requires_mcp=True,
            tools=[],
            target_service=None,
        ),
    )

    assert ctx.target_service == "gateway"


def test_target_service_before_planning_is_the_hint() -> None:
    """PLAN has not run yet, and a run that fails during routing still gets a timeline."""
    ctx = _context(service_hint="orders")

    assert ctx.route is None
    assert ctx.target_service == "orders"


def test_evidence_is_indexed_so_a_hypothesis_can_cite_it() -> None:
    ctx = _context()

    first = ctx.add_evidence(EvidenceItem(EvidenceSource.LOGS, "347 NullReferenceException"))
    second = ctx.add_evidence(EvidenceItem(EvidenceSource.METRICS, "error rate 12%"))

    ctx.hypotheses.append(Hypothesis(title="null deref", supporting_evidence=[first, second]))

    assert (first, second) == (0, 1)
    assert ctx.evidence[first].summary.startswith("347")


def test_a_weight_outside_the_range_is_refused() -> None:
    """The weight feeds the confidence score directly, so a 1.5 would inflate it silently."""
    with pytest.raises(ValueError, match="weight"):
        EvidenceItem(EvidenceSource.LOGS, "something", weight=1.5)

    with pytest.raises(ValueError, match="weight"):
        EvidenceItem(EvidenceSource.LOGS, "something", weight=-0.1)


def test_sources_seen_counts_kinds_rather_than_tools() -> None:
    """Two log tools agreeing is one source agreeing with itself, and the bonus is for diversity."""
    ctx = _context()
    ctx.add_evidence(EvidenceItem(EvidenceSource.LOGS, "a", tool="logs-mcp/get_recent_errors"))
    ctx.add_evidence(
        EvidenceItem(EvidenceSource.LOGS, "b", tool="logs-mcp/get_exception_statistics")
    )
    ctx.add_evidence(EvidenceItem(EvidenceSource.METRICS, "c", tool="metrics-mcp/get_error_rate"))

    assert ctx.sources_seen() == {EvidenceSource.LOGS, EvidenceSource.METRICS}


def test_notes_are_not_evidence() -> None:
    """A note is about the investigation; evidence is about the system under investigation."""
    ctx = _context()
    ctx.note("traces-mcp returned nothing for the window")

    assert ctx.notes == ["traces-mcp returned nothing for the window"]
    assert ctx.evidence == []


def test_the_evidence_source_values_match_the_backend_enum() -> None:
    """These strings cross the callback boundary; the C# enum has to be able to parse them."""
    assert {source.value for source in EvidenceSource} == {
        "logs",
        "metrics",
        "traces",
        "git",
        "database",
        "source_code",
        "docker",
        "historical_incident",
        "rag_document",
    }
