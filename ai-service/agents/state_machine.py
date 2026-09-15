"""The runner: nodes in, transitions out, and the guarantees that hold whatever the nodes do.

[ADR-0003](../../docs/adr/0003-custom-state-machine-over-langgraph.md) chose this over a graph
framework. The bet was that the interesting part is not edge declaration — it is that every
transition is an event a human watches arrive, and that the loop provably ends. This module is
where that bet gets paid.

**The division of labour is the design.** A node decides *what happens next*; the runner decides
*whether it is allowed to*. Nodes know about logs and hypotheses; the runner knows nothing about
either and cannot be made to, which is what lets the limits below be tested with fake nodes
before a single real one exists.

Four things the runner guarantees, none of which a node can opt out of:

*It stops.* ``max_transitions`` is a hard ceiling above the semantic limits — the tool budget and
the iteration count — and it exists because those two are only enforced where a node remembers to
consult them. Two nodes pointing at each other increment nothing, and without a ceiling that is
an investigation that runs until the process dies. Hitting it is a defect rather than an outcome,
so it ends in ``FAILED`` and says which states were repeating.

*Running out of budget is not a failure.* ``ToolBudgetExhaustedError`` ends the run in
``NEEDS_HUMAN``, because what it produced is evidence without a conclusion. That is worth showing
to somebody; a stack trace is not.

*An exception is recorded, not swallowed.* Any other exception from a node ends the run in
``FAILED`` with the reason on the context and in the final event. An investigation that dies
silently is worse than one that dies loudly, because the incident is still open either way.

*A transition to a state with no node is a defect and is treated as one.* Not "skip it and carry
on" — a plan that reaches an unimplemented state is a bug in the plan, and continuing past it
would produce a conclusion drawn from evidence that was never collected.
"""

from __future__ import annotations

import abc
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agents.context import InvestigationContext, ToolBudgetExhaustedError
from agents.states import TERMINAL_STATES, State
from observability.instruments import record_investigation, record_step

logger = logging.getLogger(__name__)

# Well above the longest legitimate run. The happy path is fourteen states; three iterations of
# the back-loop through the collectors is under fifty. A hundred is not a limit anything correct
# approaches — it is the tripwire for a cycle that increments none of the real counters.
DEFAULT_MAX_TRANSITIONS = 100


class EventType(StrEnum):
    """The ``type`` field of the callback contract in docs/planning.md §3.2.

    Fixed, because the backend switches on it to decide which table a payload lands in and the
    frontend switches on it to decide how to draw the line in the timeline.
    """

    STEP_COMPLETED = "step_completed"
    EVIDENCE_FOUND = "evidence_found"
    HYPOTHESIS = "hypothesis"
    ROOT_CAUSE = "root_cause"
    RECOMMENDATION = "recommendation"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One thing worth telling the backend about.

    No sequence number and no timestamp: both are assigned by the emitter in
    :mod:`agents.events`, because a number assigned here would be a number the runner has to keep
    consistent across retries, and it is the emitter that retries.
    """

    type: EventType
    state: State
    message: str
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class Transition:
    """What a node decided.

    ``events`` are the node's own — the evidence it found, the hypotheses it formed. The runner
    adds the ``step_completed`` event for the transition itself, so a node never has to remember
    to announce that it ran, and a node that emitted its own would produce two lines in the
    timeline for one step.

    ``message`` and ``payload`` are what that step event carries. A node describes its step; it
    does not construct the event.
    """

    next_state: State
    events: tuple[AgentEvent, ...] = ()
    message: str = ""
    payload: dict[str, Any] | None = None


class Node(abc.ABC):
    """One state's worth of work."""

    @property
    @abc.abstractmethod
    def state(self) -> State:
        """The state this node implements. Used to build the runner's table and to check it."""

    @abc.abstractmethod
    async def run(self, ctx: InvestigationContext) -> Transition:
        """Do the work and say where to go next."""


@dataclass(slots=True)
class RunResult:
    """How a run ended, for the caller that started it."""

    final_state: State
    transitions: int
    duration_ms: int
    visits: Counter[State] = field(default_factory=Counter)
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        """``COMPLETED`` only. ``NEEDS_HUMAN`` is not a success and not a failure."""
        return self.final_state is State.COMPLETED


Emitter = Callable[[AgentEvent], Awaitable[None]]


async def _discard(event: AgentEvent) -> None:
    """The default emitter. Used by tests and by any run with nothing listening."""


class StateMachine:
    """Runs nodes until one of them says the investigation is over, or a limit says it is."""

    def __init__(
        self,
        nodes: dict[State, Node],
        *,
        emit: Emitter | None = None,
        start: State = State.UNDERSTAND_INCIDENT,
        max_transitions: int = DEFAULT_MAX_TRANSITIONS,
    ) -> None:
        mislabelled = [
            f"{state} -> {node.state}" for state, node in nodes.items() if node.state is not state
        ]

        if mislabelled:
            # A node filed under the wrong key would run in a state it does not implement, and
            # the timeline would name the state it was filed under. Caught at construction
            # because the alternative is finding it in an event stream.
            raise ValueError(f"nodes registered under the wrong state: {', '.join(mislabelled)}")

        self._nodes = dict(nodes)
        self._emit = emit or _discard
        self._start = start
        self._max_transitions = max_transitions

    async def run(self, ctx: InvestigationContext) -> RunResult:
        started = time.perf_counter()
        state = self._start

        # The tally lives on the context rather than here. The runner still does not read it —
        # it counts and nothing else — but a node that needs to know whether a collector has
        # already run should not have to be told by the node that ran it.
        visits = ctx.visits
        transitions = 0

        while state not in TERMINAL_STATES:
            if transitions >= self._max_transitions:
                return await self._abort(
                    ctx,
                    state,
                    transitions,
                    started,
                    visits,
                    "the transition ceiling was reached, which means a cycle that spends no "
                    f"budget: {self._describe_cycle(visits)}",
                )

            node = self._nodes.get(state)

            if node is None:
                return await self._abort(
                    ctx,
                    state,
                    transitions,
                    started,
                    visits,
                    f"no node implements {state}",
                )

            visits[state] += 1
            step_started = time.perf_counter()

            try:
                transition = await node.run(ctx)
            except ToolBudgetExhaustedError as exc:
                # Evidence without a conclusion. A person should see what it did collect.
                return await self._stop(
                    ctx, State.NEEDS_HUMAN, state, transitions, started, visits, str(exc)
                )
            except Exception as exc:  # noqa: BLE001 - the runner is the last line before silence
                logger.exception("node %s raised", state)

                return await self._abort(
                    ctx, state, transitions, started, visits, f"{type(exc).__name__}: {exc}"
                )

            elapsed = int((time.perf_counter() - step_started) * 1000)

            # Only a step that returned a transition is measured. The three exits above end the
            # run, and their step is timed as part of the investigation total rather than as a
            # step that completed — the state-duration series answers "where do the minutes go
            # on a working investigation", and a state that always dies in it would otherwise
            # look like the fastest one.
            record_step(state=state, duration_ms=elapsed)

            await self._emit(
                AgentEvent(
                    type=EventType.STEP_COMPLETED,
                    state=state,
                    message=transition.message or f"{state} completed",
                    payload={
                        **(transition.payload or {}),
                        "next_state": transition.next_state,
                        "duration_ms": elapsed,
                    },
                )
            )

            for event in transition.events:
                await self._emit(event)

            state = transition.next_state
            transitions += 1

        return await self._stop(ctx, state, state, transitions, started, visits, None)

    async def _stop(
        self,
        ctx: InvestigationContext,
        final: State,
        reached_from: State,
        transitions: int,
        started: float,
        visits: Counter[State],
        reason: str | None,
    ) -> RunResult:
        if reason:
            ctx.note(reason)

        await self._emit(
            AgentEvent(
                type=EventType.FAILED if final is State.FAILED else EventType.COMPLETED,
                state=final,
                message=reason or f"investigation ended in {final}",
                payload={"reached_from": reached_from, "transitions": transitions},
            )
        )

        result = RunResult(
            final_state=final,
            transitions=transitions,
            duration_ms=int((time.perf_counter() - started) * 1000),
            # A copy: the result is a snapshot of how the run went, and the context it came from
            # is still writable by whoever asked for the run.
            visits=Counter(visits),
            failure_reason=reason,
        )

        # Every run ends here — the terminal state, the budget, the ceiling and an exception from
        # a node all arrive through this one method — so this is the only place the count of
        # investigations can be kept without a way to miss one.
        record_investigation(final_state=str(final), duration_ms=result.duration_ms)

        return result

    async def _abort(
        self,
        ctx: InvestigationContext,
        reached_from: State,
        transitions: int,
        started: float,
        visits: Counter[State],
        reason: str,
    ) -> RunResult:
        return await self._stop(
            ctx, State.FAILED, reached_from, transitions, started, visits, reason
        )

    @staticmethod
    def _describe_cycle(visits: Counter[State]) -> str:
        """The states that ran most, which is where a loop that will not end is.

        Put in the failure reason rather than only the log, because this failure reaches a human
        through the timeline and "it looped" without saying where is not actionable.
        """
        return ", ".join(f"{state} x{count}" for state, count in visits.most_common(3))
