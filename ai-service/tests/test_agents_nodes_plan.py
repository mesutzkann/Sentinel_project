"""The planner: which collectors an intent proposes, and what the router's flags do to them."""

from __future__ import annotations

import pytest

from agents.context import InvestigationContext
from agents.nodes.plan import INTENT_COLLECTORS, PlanNode, plan_for
from agents.nodes.understand import UnderstandIncidentNode
from agents.states import COLLECTOR_STATES, State
from routing.base import Router, RouterError
from routing.rule_router import RuleBasedRouter
from routing.schema import Intent, RouteDecision


class _FixedRouter(Router):
    def __init__(self, decision: RouteDecision) -> None:
        self._decision = decision

    @property
    def name(self) -> str:
        return "fixed"

    async def route(self, query: str, *, service_hint: str | None = None) -> RouteDecision:
        return self._decision


class _BrokenRouter(Router):
    @property
    def name(self) -> str:
        return "broken"

    async def route(self, query: str, *, service_hint: str | None = None) -> RouteDecision:
        raise RouterError("ollama is not reachable")


def _context(query: str = "why is orders failing", **kwargs: object) -> InvestigationContext:
    defaults: dict[str, object] = {
        "investigation_id": "11111111-1111-1111-1111-111111111111",
        "incident_code": "INC-00142",
        "query": query,
    }

    return InvestigationContext(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_every_intent_proposes_at_least_one_collector() -> None:
    """An intent that plans nothing would reason on the incident alone, silently."""
    assert set(INTENT_COLLECTORS) == set(Intent)

    for intent, collectors in INTENT_COLLECTORS.items():
        assert collectors, intent


def test_only_real_collector_states_are_proposed() -> None:
    for intent, collectors in INTENT_COLLECTORS.items():
        for state in collectors:
            assert state in COLLECTOR_STATES, f"{intent}: {state}"


def test_no_intent_proposes_a_collector_twice() -> None:
    for intent, collectors in INTENT_COLLECTORS.items():
        assert len(set(collectors)) == len(collectors), intent


def test_reading_code_is_never_proposed_by_breadth() -> None:
    """It is the most expensive evidence there is, and before a hypothesis there is nothing to
    look for. Only the two intents that are actually about code ask for it."""
    asking = {i for i, states in INTENT_COLLECTORS.items() if State.INSPECT_CODE in states}

    assert asking == {Intent.CODE_LOOKUP, Intent.CONFIG_LOOKUP}


def test_a_documentation_question_keeps_only_the_collector_that_needs_no_server() -> None:
    plan = plan_for(Intent.KNOWLEDGE_QUESTION, requires_mcp=False, requires_rag=True)

    assert plan == [State.SEARCH_HISTORY]


def test_no_retrieval_drops_history_and_keeps_the_rest() -> None:
    plan = plan_for(Intent.ERROR_ANALYSIS, requires_mcp=True, requires_rag=False)

    assert State.SEARCH_HISTORY not in plan
    assert plan == [State.COLLECT_LOGS, State.COLLECT_METRICS]


def test_a_router_that_contradicts_itself_plans_less_rather_than_erroring() -> None:
    """In Phase 8 these booleans come out of a 1.5B model; an inconsistent answer is the failure
    mode to design for, and it should shrink the plan rather than stop the run."""
    plan = plan_for(Intent.FULL_INVESTIGATION, requires_mcp=False, requires_rag=False)

    assert plan == []


async def test_the_node_records_the_route_and_walks_the_plan() -> None:
    node = PlanNode(RuleBasedRouter())
    ctx = _context("payments servisini arastir")

    transition = await node.run(ctx)

    assert ctx.route is not None
    assert ctx.route.intent is Intent.FULL_INVESTIGATION
    assert ctx.target_service == "payments"
    # The first collector is the transition; the rest stay queued on the context.
    assert transition.next_state is State.COLLECT_LOGS
    assert ctx.plan[0] is State.COLLECT_METRICS


async def test_the_step_payload_says_what_was_planned_and_what_was_skipped() -> None:
    """The timeline is how a human checks that the agent looked in the right places."""
    node = PlanNode(
        _FixedRouter(
            RouteDecision(
                intent=Intent.FULL_INVESTIGATION,
                requires_rag=False,
                requires_mcp=True,
                target_service="orders",
            )
        )
    )

    transition = await node.run(_context())
    payload = transition.payload or {}

    assert payload["intent"] is Intent.FULL_INVESTIGATION
    assert payload["router"] == "fixed"
    assert payload["target_service"] == "orders"
    assert State.SEARCH_HISTORY.value in payload["skipped"]
    assert State.SEARCH_HISTORY.value not in payload["plan"]
    assert payload["plan"][0] == State.COLLECT_LOGS.value


async def test_an_empty_plan_goes_straight_to_reasoning_and_says_so() -> None:
    node = PlanNode(
        _FixedRouter(
            RouteDecision(
                intent=Intent.KNOWLEDGE_QUESTION, requires_rag=False, requires_mcp=False
            )
        )
    )
    ctx = _context()

    transition = await node.run(ctx)

    assert transition.next_state is State.GENERATE_HYPOTHESES
    assert any("collects nothing" in note for note in ctx.notes)


async def test_a_router_failure_asks_for_a_human_rather_than_collecting_everything() -> None:
    """A run with no route has no plan. Collecting everything and calling it a decision would be
    the one outcome worse than stopping."""
    node = PlanNode(_BrokenRouter())
    ctx = _context()

    transition = await node.run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert ctx.route is None
    assert any("routing failed" in note for note in ctx.notes)


async def test_understand_records_the_question_before_anything_is_collected() -> None:
    ctx = _context(service_hint="orders")

    transition = await UnderstandIncidentNode().run(ctx)

    assert transition.next_state is State.PLAN
    assert "INC-00142" in transition.message
    assert (transition.payload or {})["service_hint"] == "orders"


@pytest.mark.parametrize("intent", list(Intent))
def test_the_full_plan_for_every_intent_is_orderable(intent: Intent) -> None:
    """Cheap and broad before narrow and deep, so a run that exhausts its budget has still seen
    the shape of the failure. Encoded as: logs and metrics never come after code."""
    plan = plan_for(intent, requires_mcp=True, requires_rag=True)

    if State.INSPECT_CODE in plan:
        code_at = plan.index(State.INSPECT_CODE)

        for cheap in (State.COLLECT_LOGS, State.COLLECT_METRICS):
            if cheap in plan:
                assert plan.index(cheap) < code_at
