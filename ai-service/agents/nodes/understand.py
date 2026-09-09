"""The first state: say out loud what is being asked, before anything is collected.

It does very little and it is not a placeholder. The timeline's first line is what a human reads
to check that the agent understood the question at all, and an investigation whose first visible
act is "queried Loki" gives them nothing to check that against. It is also the step that makes a
run with no service reproducible later: the question and the service the collectors will point at
are recorded before any of them run, so a wrong answer can be traced to a wrong target rather
than guessed at.
"""

from __future__ import annotations

from agents.context import InvestigationContext
from agents.state_machine import Node, Transition
from agents.states import State


class UnderstandIncidentNode(Node):
    """Records the question and the incident it is about."""

    @property
    def state(self) -> State:
        return State.UNDERSTAND_INCIDENT

    async def run(self, ctx: InvestigationContext) -> Transition:
        target = ctx.target_service or "no service named yet"
        message = f"Investigating {ctx.incident_code}: {ctx.query} ({target})"

        return Transition(
            next_state=State.PLAN,
            message=message,
            payload={
                "incident_code": ctx.incident_code,
                "query": ctx.query,
                "service_hint": ctx.service_hint,
                "tool_budget": ctx.tool_budget,
            },
        )
