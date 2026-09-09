"""What every collector does the same way, so that each one only writes down what it knows.

A collector is four decisions: which tools to call, whether it can call them at all, what each
result means, and what that meaning is worth. The first, third and fourth are specific to the
signal and live in the subclasses. The second is identical everywhere and is here.

**Summarising is deterministic, and that is the load-bearing decision of this slice.** Twenty-five
tool outputs do not fit in an 8k window, so something has to compress them, and the obvious
something is the language model. It is the wrong one. The MCP servers already return interpreted
structured output — ``get_recent_errors`` comes back with ``found``, ``window``, ``items``, a
``note`` and an ``interpretation`` — so an LLM pass over that dict would add a second's latency
and a chance to say something the dict does not, twenty-five times, to reword text a server
already wrote. The collectors read those fields and compose a line. When Phase 7.4 needs prose it
has the raw dict to work from, and the agent's own reasoning happens where reasoning belongs.

Three rules the base enforces, each of which a collector would otherwise have to remember:

*A failed tool is not a fact.* A refusal, a timeout or an unreachable server becomes a note on
the investigation, never evidence. Evidence is what the system said about itself; "Loki did not
answer" is about the investigation, and a hypothesis built on it would be reasoning about the
observability stack while an incident is open.

*Budget is spent before the calls, for all of them at once.* A collector that got halfway would
have to decide what a half-collected signal means, and nothing downstream could tell the
difference between "the traces were clean" and "we could not afford to look".

*Finding nothing is a result.* An empty collector still records that it looked and came back
empty, because that is exactly what separates a slow query with no errors from a pool exhaustion
with many, and a run whose timeline simply omits the step it could not fill is one a human cannot
audit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from agents.context import EvidenceItem, EvidenceSource, InvestigationContext
from agents.state_machine import AgentEvent, EventType, Node, Transition
from agents.states import State
from mcp_client.client import McpClient, ToolCallResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """One call a collector wants to make."""

    qualified_name: str
    arguments: dict[str, Any]

    @property
    def tool(self) -> str:
        _, _, name = self.qualified_name.partition("/")

        return name or self.qualified_name


class CollectorNode(Node):
    """A state that calls tools and turns what comes back into evidence."""

    #: Which kind of signal this collector produces. One per collector, because the confidence
    #: score pays a bonus for agreement *across* sources and two log tools are not two sources.
    source: EvidenceSource = EvidenceSource.LOGS

    def __init__(self, client: McpClient) -> None:
        self._client = client

    # ---- what a subclass decides -------------------------------------------------------

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        """The calls to make for this investigation, or an empty list to skip the state.

        Returning nothing is how a collector says "not applicable here" — the log collector with
        no service to look at, for instance. It is a legitimate answer and it is recorded as one.
        """
        raise NotImplementedError

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        """Turn one successful tool result into a fact, or ``None`` to record nothing.

        ``None`` is for output that says nothing either way. It is not the same as an empty
        result, which usually says a great deal.
        """
        raise NotImplementedError

    def skipped_reason(self, ctx: InvestigationContext) -> str:
        """Why :meth:`plan_calls` returned nothing. Goes on the timeline."""
        return f"{self.state} had nothing applicable to call"

    # ---- what every collector does the same ---------------------------------------------

    async def run(self, ctx: InvestigationContext) -> Transition:
        requests = self.plan_calls(ctx)

        if not requests:
            reason = self.skipped_reason(ctx)
            ctx.note(reason)

            return self.advance(ctx, message=reason, payload={"skipped": True})

        # All of them, before any of them. A partly-afforded collector produces a signal nothing
        # downstream can interpret.
        ctx.spend_tool_call(len(requests))

        events: list[AgentEvent] = []
        found = 0
        failures: list[str] = []

        for request in requests:
            result = await self._client.call(request.qualified_name, request.arguments)

            if not result.success:
                note = f"{request.qualified_name} failed: {result.error}"
                ctx.note(note)
                failures.append(request.tool)
                logger.warning("collector %s: %s", self.state, note)
                continue

            item = self.summarise(result)

            if item is None:
                continue

            index = ctx.add_evidence(item)
            found += 1
            events.append(
                AgentEvent(
                    type=EventType.EVIDENCE_FOUND,
                    state=self.state,
                    message=item.summary,
                    payload={
                        "index": index,
                        "source": item.source,
                        "weight": item.weight,
                        "tool": item.tool,
                        "latency_ms": result.latency_ms,
                    },
                )
            )

        message = self._describe(found, len(requests), failures)

        return self.advance(
            ctx,
            message=message,
            events=tuple(events),
            payload={
                "tools_called": [r.qualified_name for r in requests],
                "evidence_added": found,
                "failures": failures,
                "budget_remaining": ctx.budget_remaining,
            },
        )

    def advance(
        self,
        ctx: InvestigationContext,
        *,
        message: str,
        events: tuple[AgentEvent, ...] = (),
        payload: dict[str, Any] | None = None,
    ) -> Transition:
        """Go to the next collector on the plan, or to reasoning when the plan is done.

        Shared because every collector ends the same way, and a collector that hard-coded its
        successor would make the plan a lie: PLAN decides the order, not the collectors.
        """
        return Transition(
            next_state=ctx.advance() or State.GENERATE_HYPOTHESES,
            message=message,
            events=events,
            payload=payload,
        )

    def _describe(self, found: int, attempted: int, failures: list[str]) -> str:
        parts = [f"{found} fact(s) from {attempted} call(s)"]

        if failures:
            parts.append(f"{len(failures)} failed: {', '.join(failures)}")

        return f"{self.state}: {'; '.join(parts)}"


def window(ctx: InvestigationContext) -> int:
    """The minutes every collector asks for. One place, so the signals line up in time."""
    return ctx.window_minutes
