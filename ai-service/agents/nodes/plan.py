"""Deciding which collectors run, which is the only place the router's answer has consequences.

docs/planning.md §7: "PLAN state'i router çıktısına göre hangi COLLECT_* adımlarının atlanacağına
karar verir." Everything downstream reads evidence; this is the state that decides what evidence
there will be, so a wrong plan is not recoverable later — the reasoning nodes will be confident
about whatever they were given.

The derivation is deliberately in two parts, and the second is what stops the router's booleans
being decorative:

1. The intent proposes a candidate list. ``LOG_QUERY`` proposes the log collector; a full
   investigation proposes nearly all of them.
2. ``requires_mcp`` and ``requires_rag`` filter it. A question the router marked as needing no
   live signal keeps only SEARCH_HISTORY, which reads the knowledge base; one marked as needing
   no retrieval drops SEARCH_HISTORY and keeps the rest.

So a router that says ``KNOWLEDGE_QUESTION`` with ``requires_mcp=True`` still gets a plan that
touches the live system, and one that contradicts itself produces a smaller plan rather than an
error. That is on purpose: in Phase 8 these three fields come out of a 1.5B model, and the
failure mode to design for is an inconsistent answer, not a missing one.

**INSPECT_CODE is never proposed by breadth.** It is on the plan only when the intent is
specifically about code or configuration. Reading source is the most expensive evidence the agent
can gather in prompt tokens, and a full investigation that reads code before it has a hypothesis
is reading it without knowing what to look for.
"""

from __future__ import annotations

from agents.context import InvestigationContext
from agents.state_machine import Node, Transition
from agents.states import State
from routing.base import Router, RouterError
from routing.schema import Intent

# Intent -> the collectors it proposes, in the order they should run. Order matters beyond
# tidiness: the cheap and broad signals come before the narrow ones, so that a run which
# exhausts its budget has still seen the shape of the failure rather than one deep slice of it.
INTENT_COLLECTORS: dict[Intent, tuple[State, ...]] = {
    Intent.FULL_INVESTIGATION: (
        State.COLLECT_LOGS,
        State.COLLECT_METRICS,
        State.COLLECT_TRACES,
        State.COLLECT_DATABASE,
        State.CHECK_DEPLOYMENTS,
        State.SEARCH_HISTORY,
    ),
    Intent.ERROR_ANALYSIS: (
        State.COLLECT_LOGS,
        State.COLLECT_METRICS,
        State.SEARCH_HISTORY,
    ),
    Intent.LOG_QUERY: (State.COLLECT_LOGS,),
    Intent.METRIC_QUERY: (State.COLLECT_METRICS,),
    Intent.TRACE_QUERY: (State.COLLECT_TRACES,),
    Intent.PERFORMANCE_ANALYSIS: (
        State.COLLECT_METRICS,
        State.COLLECT_TRACES,
        State.COLLECT_DATABASE,
        State.SEARCH_HISTORY,
    ),
    Intent.DATABASE_HEALTH: (
        State.COLLECT_DATABASE,
        State.COLLECT_LOGS,
        State.SEARCH_HISTORY,
    ),
    Intent.DEPLOYMENT_CHECK: (State.CHECK_DEPLOYMENTS,),
    Intent.CODE_LOOKUP: (State.INSPECT_CODE, State.SEARCH_HISTORY),
    Intent.CONFIG_LOOKUP: (State.INSPECT_CODE, State.SEARCH_HISTORY),
    Intent.SERVICE_TOPOLOGY: (State.COLLECT_TRACES, State.SEARCH_HISTORY),
    Intent.HISTORICAL_SIMILARITY: (State.SEARCH_HISTORY,),
    Intent.KNOWLEDGE_QUESTION: (State.SEARCH_HISTORY,),
    Intent.REMEDIATION_QUESTION: (State.SEARCH_HISTORY,),
    Intent.GENERAL_QUESTION: (
        State.COLLECT_LOGS,
        State.COLLECT_METRICS,
        State.SEARCH_HISTORY,
    ),
}

# The one collector that needs no MCP server: it reads the knowledge base, which is this
# service's own PostgreSQL. It is therefore what survives when every MCP server is unreachable,
# and the reason a stack with nothing running still answers "have we seen this before".
_RAG_COLLECTOR = State.SEARCH_HISTORY


class PlanNode(Node):
    """Routes the question, then turns the route into an ordered list of collectors."""

    def __init__(self, router: Router) -> None:
        self._router = router

    @property
    def state(self) -> State:
        return State.PLAN

    async def run(self, ctx: InvestigationContext) -> Transition:
        try:
            route = await self._router.route(ctx.query, service_hint=ctx.service_hint)
        except RouterError as exc:
            # Not fatal. A run with no route is a run with no plan, and the honest thing is to
            # say so and stop rather than to collect everything and call it a decision.
            ctx.note(f"routing failed: {exc}")

            return Transition(
                next_state=State.NEEDS_HUMAN,
                message=f"Could not route the question: {exc}",
            )

        ctx.route = route
        ctx.plan = plan_for(
            route.intent,
            requires_mcp=route.requires_mcp,
            requires_rag=route.requires_rag,
        )

        skipped = [
            state.value for state in INTENT_COLLECTORS[route.intent] if state not in ctx.plan
        ]

        first = ctx.advance()

        if first is None:
            # Nothing to collect. The reasoning nodes will have only the incident to work with,
            # which is a real state of affairs and is visible as such rather than silently empty.
            ctx.note("the plan collects nothing; reasoning will run on the incident alone")

        message = (
            f"{route.intent} on {route.target_service or 'no single service'}: "
            f"{len(ctx.plan) + (1 if first else 0)} collector(s)"
        )

        return Transition(
            next_state=first or State.GENERATE_HYPOTHESES,
            message=message,
            payload={
                "intent": route.intent,
                "router": self._router.name,
                "target_service": route.target_service,
                "requires_rag": route.requires_rag,
                "requires_mcp": route.requires_mcp,
                "suggested_tools": route.tools,
                "plan": ([first.value, *(s.value for s in ctx.plan)] if first else []),
                "skipped": skipped,
            },
        )


def plan_for(intent: Intent, *, requires_mcp: bool, requires_rag: bool) -> list[State]:
    """The collectors for an intent, filtered by what the router said it needs.

    Split out from the node so the table can be tested without a router, and so that
    COLLECT_ADDITIONAL_EVIDENCE in Phase 7.4 can ask the same question about a different intent
    without going through PLAN again.
    """
    candidates = INTENT_COLLECTORS[intent]

    return [
        state
        for state in candidates
        if (requires_rag if state is _RAG_COLLECTOR else requires_mcp)
    ]
