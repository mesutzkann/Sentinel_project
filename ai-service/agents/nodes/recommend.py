"""RECOMMEND_FIX: what to do about it, written down and not done.

This state is only reached by a conclusion the critic accepted and the confidence score cleared,
which is the whole of its safety story: a recommendation exists because something already decided
the diagnosis was trustworthy, rather than being produced alongside every guess.

**Nothing here executes and nothing here is arguable about approval.** ``requires_approval`` is
True on every recommendation this node makes and no argument changes it. Phase 10 adds the
approval screen and the execution path; until then these are sentences on a page, and a field
that could be False would be a field somebody later trusts.

**The model names a tool and never its arguments.** ``tool_name`` is a hint about which MCP tool
would carry the action out — it goes on the record so the approval screen can offer it — and the
arguments are left empty deliberately. Arguments are what makes a tool call *do* something to a
running service, and a 3B model filling in a service name and a pool size that a human then waves
through is the failure this project keeps designing against. ``mcp_client.policy`` would refuse an
unknown tool anyway; the point is not to write the plausible-looking command down at all.
"""

from __future__ import annotations

import logging

from agents.context import InvestigationContext, Recommendation
from agents.nodes.reasoning import ReasoningNode, render_evidence, render_incident
from agents.schemas import FixPlan
from agents.state_machine import AgentEvent, EventType, Transition
from agents.states import State
from llm.structured import StructuredOutputError

logger = logging.getLogger(__name__)


class RecommendFixNode(ReasoningNode):
    """Asks for the actions that would address the accepted root cause."""

    prompt_name = "recommend_fix"

    @property
    def state(self) -> State:
        return State.RECOMMEND_FIX

    async def run(self, ctx: InvestigationContext) -> Transition:
        root_cause = ctx.root_cause

        if root_cause is None:
            reason = "there is no accepted root cause to recommend a fix for"
            ctx.note(reason)

            return Transition(next_state=State.NEEDS_HUMAN, message=reason)

        try:
            result = await self.ask(
                ctx,
                FixPlan,
                incident=render_incident(ctx),
                evidence=render_evidence(ctx),
                root_cause=(
                    f"{root_cause.title} [{root_cause.category or 'uncategorised'}]\n"
                    f"{root_cause.explanation}"
                ),
                confidence=f"{root_cause.confidence:.2f}",
            )
        except StructuredOutputError as exc:
            # The conclusion survives. A run that diagnosed the incident and could not phrase a
            # fix has done most of the work, and ending it in FAILED would throw that away;
            # `stalled` ends it in NEEDS_HUMAN with the root cause already on the record.
            return self.stalled(ctx, exc, producing="fix plan")

        ctx.recommendations = [
            Recommendation(
                action_code=action.action_code,
                description=action.description,
                tool_name=action.tool_name,
                requires_approval=True,
            )
            for action in result.value.actions
        ]

        events = tuple(
            AgentEvent(
                type=EventType.RECOMMENDATION,
                state=self.state,
                message=f"{recommendation.action_code}: {recommendation.description}",
                payload={
                    "action_code": recommendation.action_code,
                    "description": recommendation.description,
                    "tool_name": recommendation.tool_name,
                    "tool_args": recommendation.tool_args,
                    "requires_approval": True,
                },
            )
            for recommendation in ctx.recommendations
        )

        message = (
            f"{len(ctx.recommendations)} recommendation(s), all requiring approval: "
            f"{', '.join(r.action_code for r in ctx.recommendations)}"
        )

        return Transition(
            next_state=State.COMPLETED,
            message=message,
            events=events,
            payload={
                **self.usage(result),
                "recommendations": [r.action_code for r in ctx.recommendations],
                "confidence": root_cause.confidence,
            },
        )
