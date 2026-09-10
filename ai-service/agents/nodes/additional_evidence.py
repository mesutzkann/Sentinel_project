"""COLLECT_ADDITIONAL_EVIDENCE: the one edge in the machine that goes backwards.

docs/planning.md §7: if a hypothesis is missing a signal, the machine may return to a collector,
bounded by ``max_iterations = 3`` and the tool budget of 25. This is that decision, and it is
deterministic — no model call. What it decides is cheap to state: *a hypothesis named a scenario
whose discriminating signal was never collected, so go and collect it.*

**One gap, one collector, per visit.** A hypothesis about connection pool exhaustion needs the
connection count; requeueing the database, trace and log collectors at once because three
hypotheses each wanted something would spend most of the remaining budget before the next round
of reasoning, which is the budget this loop exists to conserve. The first gap in the model's own
order is taken, because that order is its plausibility ranking and the most plausible hypothesis
is the one worth spending a tool call on.

**A collector that has already run is never requeued.** It would ask the same tools the same
question over the same window and get the same answer, for three more calls of the budget. The
context's visit tally is what makes that knowable — see ``InvestigationContext.visits``.

**A missing SEARCH_HISTORY is not a gap.** The router decided whether the knowledge base was
relevant and PLAN acted on it; re-adding it here would be this node quietly overruling the plan
for every investigation, rather than filling a hole a hypothesis actually exposed.

The machine has to be built with *every* collector registered, not only the ones the plan
scheduled: this node can send it to one the plan never had. The runner treats a transition into
an unimplemented state as a defect, so a composition root that registers only the planned
collectors fails loudly on the first back-loop rather than quietly skipping it.
"""

from __future__ import annotations

import logging

from agents.categories import ScenarioCategory
from agents.context import Hypothesis, InvestigationContext
from agents.state_machine import Node, Transition
from agents.states import State

logger = logging.getLogger(__name__)

# The signal that would settle each scenario — one collector, not the several that would be
# *relevant*. A slow query missing an index shows up in traces and in logs, but what decides it is
# `get_slow_queries`, and spending the loop on corroboration rather than discrimination is how a
# budget disappears without the answer getting any clearer.
CATEGORY_SIGNALS: dict[str, State] = {
    ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION: State.COLLECT_DATABASE,
    ScenarioCategory.DB_SLOW_QUERY_MISSING_INDEX: State.COLLECT_DATABASE,
    ScenarioCategory.DB_DEADLOCK: State.COLLECT_DATABASE,
    ScenarioCategory.DB_N_PLUS_ONE_QUERY: State.COLLECT_DATABASE,
    # An exception's shape and its stack are in the logs; a metric only says how many.
    ScenarioCategory.NULL_REFERENCE_EXCEPTION: State.COLLECT_LOGS,
    ScenarioCategory.DIVIDE_BY_ZERO_EDGE_CASE: State.COLLECT_LOGS,
    ScenarioCategory.MEMORY_LEAK: State.COLLECT_METRICS,
    # Configuration scenarios are decided by what the configured value *is*, which is in the
    # repository. The symptoms are everywhere; the value is in one place.
    ScenarioCategory.TIMEOUT_TOO_LOW: State.INSPECT_CODE,
    ScenarioCategory.WRONG_CONNECTION_STRING: State.INSPECT_CODE,
    # A retry storm is visible as one logical call becoming many spans, which is a trace shape.
    ScenarioCategory.RETRY_STORM: State.COLLECT_TRACES,
    ScenarioCategory.DOWNSTREAM_LATENCY_CASCADE: State.COLLECT_TRACES,
    ScenarioCategory.EXTERNAL_DEPENDENCY_UNAVAILABLE: State.COLLECT_LOGS,
    ScenarioCategory.CIRCUIT_BREAKER_STUCK_OPEN: State.COLLECT_LOGS,
    ScenarioCategory.BAD_DEPLOYMENT_REGRESSION: State.CHECK_DEPLOYMENTS,
    ScenarioCategory.CPU_SATURATION: State.COLLECT_METRICS,
}

# The most tool calls any one collector makes (COLLECT_DATABASE makes three). Requeueing with
# less than this in the budget would send the machine to a collector that raises on arrival, and
# ``ToolBudgetExhaustedError`` ends the run in NEEDS_HUMAN — throwing away a conclusion that was
# one ranking away, to ask a question there was never room for.
MIN_BUDGET_FOR_COLLECTOR = 3


class CollectAdditionalEvidenceNode(Node):
    """Decides whether a hypothesis is missing a signal worth going back for."""

    @property
    def state(self) -> State:
        return State.COLLECT_ADDITIONAL_EVIDENCE

    async def run(self, ctx: InvestigationContext) -> Transition:
        gap = self._first_gap(ctx)

        if gap is None:
            return self._proceed(
                ctx,
                "every hypothesis rests on a signal that was collected",
                {"requeued": None},
            )

        hypothesis, collector = gap

        # Only now, because an iteration is spent by *going back*, and a round that found no gap
        # has not used one.
        if not ctx.begin_iteration():
            return self._proceed(
                ctx,
                f"{collector} would settle '{hypothesis.title}', but the iteration limit "
                f"({ctx.max_iterations}) is reached",
                {"requeued": None, "wanted": collector.value, "reason": "iterations"},
            )

        if ctx.budget_remaining < MIN_BUDGET_FOR_COLLECTOR:
            return self._proceed(
                ctx,
                f"{collector} would settle '{hypothesis.title}', but only "
                f"{ctx.budget_remaining} tool call(s) remain",
                {"requeued": None, "wanted": collector.value, "reason": "budget"},
            )

        ctx.requeue(collector)
        message = (
            f"'{hypothesis.title}' needs {collector}, which has not run; "
            f"iteration {ctx.iteration} of {ctx.max_iterations}"
        )
        ctx.note(message)

        return Transition(
            next_state=collector,
            message=message,
            payload={
                "requeued": collector.value,
                "for_hypothesis": hypothesis.title,
                "category": hypothesis.category,
                "iteration": ctx.iteration,
                "budget_remaining": ctx.budget_remaining,
            },
        )

    @staticmethod
    def _first_gap(ctx: InvestigationContext) -> tuple[Hypothesis, State] | None:
        """The most plausible hypothesis whose discriminating collector has not run.

        A hypothesis with no category is skipped rather than guessed at: there is no signal to
        name for an explanation that did not name a failure mode, and picking a collector for it
        would be this node inventing the diagnosis the model declined to make.
        """
        for hypothesis in ctx.hypotheses:
            if hypothesis.category is None:
                continue

            collector = CATEGORY_SIGNALS.get(hypothesis.category)

            if collector is None or ctx.has_run(collector):
                continue

            return hypothesis, collector

        return None

    def _proceed(
        self,
        ctx: InvestigationContext,
        reason: str,
        payload: dict[str, object],
    ) -> Transition:
        """Go on to ranking, saying why no more evidence is being collected.

        Every one of these is a sentence a human may need: a conclusion drawn without the signal
        that would have settled it should say so on the timeline, not only in a log line.
        """
        ctx.note(reason)

        return Transition(
            next_state=State.RANK_HYPOTHESES,
            message=reason,
            payload={**payload, "hypotheses": len(ctx.hypotheses)},
        )
