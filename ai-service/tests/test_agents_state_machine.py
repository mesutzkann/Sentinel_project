"""The runner's guarantees, tested with nodes that do nothing but transition.

Fake nodes are the right tool here and not a shortcut. The properties being checked — that the
loop ends, that a budget exhaustion is not a failure, that an exception is recorded — are
properties of the runner, and a test that reached Loki to check them would be testing the
collectors instead.
"""

from __future__ import annotations

import pytest

from agents.context import InvestigationContext, ToolBudgetExhaustedError
from agents.state_machine import (
    AgentEvent,
    EventType,
    Node,
    StateMachine,
    Transition,
)
from agents.states import TERMINAL_STATES, State


class _Fixed(Node):
    """Goes where it was told, every time."""

    def __init__(self, state: State, nxt: State, events: tuple[AgentEvent, ...] = ()) -> None:
        self._state = state
        self._next = nxt
        self._events = events

    @property
    def state(self) -> State:
        return self._state

    async def run(self, ctx: InvestigationContext) -> Transition:
        return Transition(next_state=self._next, events=self._events, message=f"ran {self._state}")


class _Raises(Node):
    def __init__(self, state: State, exc: Exception) -> None:
        self._state = state
        self._exc = exc

    @property
    def state(self) -> State:
        return self._state

    async def run(self, ctx: InvestigationContext) -> Transition:
        raise self._exc


def _context() -> InvestigationContext:
    return InvestigationContext(
        investigation_id="11111111-1111-1111-1111-111111111111",
        incident_code="INC-00142",
        query="why is orders failing",
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)


async def test_a_straight_run_reaches_completed() -> None:
    machine = StateMachine(
        {
            State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.PLAN),
            State.PLAN: _Fixed(State.PLAN, State.COMPLETED),
        }
    )

    result = await machine.run(_context())

    assert result.final_state is State.COMPLETED
    assert result.succeeded is True
    assert result.transitions == 2
    assert result.visits[State.PLAN] == 1


async def test_two_nodes_pointing_at_each_other_do_not_run_forever() -> None:
    """The reason the ceiling exists: this cycle spends no budget and burns no iteration.

    The semantic limits are only enforced where a node consults them, so a pair of nodes that
    consult neither would loop until the process died. It ends in FAILED rather than NEEDS_HUMAN
    because it is a defect in the nodes, not an investigation that ran out of room.
    """
    machine = StateMachine(
        {
            State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.PLAN),
            State.PLAN: _Fixed(State.PLAN, State.UNDERSTAND_INCIDENT),
        },
        max_transitions=10,
    )

    result = await machine.run(_context())

    assert result.final_state is State.FAILED
    assert result.transitions == 10
    assert "cycle" in (result.failure_reason or "")
    # And it names where the loop was, so the failure is actionable from the timeline alone.
    assert "PLAN" in (result.failure_reason or "")


async def test_running_out_of_tool_budget_asks_for_a_human_rather_than_failing() -> None:
    """It has evidence and no conclusion. That is worth showing to somebody."""
    machine = StateMachine(
        {
            State.UNDERSTAND_INCIDENT: _Raises(
                State.UNDERSTAND_INCIDENT, ToolBudgetExhaustedError("3 requested, 1 left")
            )
        }
    )
    ctx = _context()

    result = await machine.run(ctx)

    assert result.final_state is State.NEEDS_HUMAN
    assert result.succeeded is False
    assert "1 left" in (result.failure_reason or "")
    assert any("1 left" in note for note in ctx.notes)


async def test_any_other_exception_ends_the_run_and_is_recorded() -> None:
    machine = StateMachine(
        {State.UNDERSTAND_INCIDENT: _Raises(State.UNDERSTAND_INCIDENT, KeyError("service"))}
    )
    ctx = _context()

    result = await machine.run(ctx)

    assert result.final_state is State.FAILED
    assert "KeyError" in (result.failure_reason or "")
    assert ctx.notes


async def test_a_transition_to_an_unimplemented_state_is_a_defect() -> None:
    """Not skipped. A plan that reaches a state nothing implements collected nothing there, and
    carrying on would draw a conclusion from evidence that was never gathered."""
    machine = StateMachine(
        {State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.COLLECT_TRACES)}
    )

    result = await machine.run(_context())

    assert result.final_state is State.FAILED
    assert "COLLECT_TRACES" in (result.failure_reason or "")


def test_a_node_registered_under_the_wrong_state_is_refused_at_construction() -> None:
    """Otherwise it runs in a state it does not implement and the timeline names the wrong one."""
    with pytest.raises(ValueError, match="wrong state"):
        StateMachine({State.PLAN: _Fixed(State.COLLECT_LOGS, State.COMPLETED)})


async def test_every_step_produces_exactly_one_step_event() -> None:
    """A node does not announce that it ran; the runner does. Two lines for one step is a bug."""
    recorder = _Recorder()
    machine = StateMachine(
        {
            State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.PLAN),
            State.PLAN: _Fixed(State.PLAN, State.COMPLETED),
        },
        emit=recorder,
    )

    await machine.run(_context())

    steps = [e for e in recorder.events if e.type is EventType.STEP_COMPLETED]

    assert [e.state for e in steps] == [State.UNDERSTAND_INCIDENT, State.PLAN]
    assert [e.message for e in steps] == ["ran UNDERSTAND_INCIDENT", "ran PLAN"]


async def test_a_nodes_own_events_are_emitted_after_its_step() -> None:
    """Order matters to the timeline: the step is the heading, the evidence sits under it."""
    recorder = _Recorder()
    found = AgentEvent(
        type=EventType.EVIDENCE_FOUND, state=State.COLLECT_LOGS, message="347 exceptions"
    )
    machine = StateMachine(
        {State.COLLECT_LOGS: _Fixed(State.COLLECT_LOGS, State.COMPLETED, events=(found,))},
        start=State.COLLECT_LOGS,
        emit=recorder,
    )

    await machine.run(_context())

    kinds = [e.type for e in recorder.events]

    assert kinds == [EventType.STEP_COMPLETED, EventType.EVIDENCE_FOUND, EventType.COMPLETED]


async def test_the_final_event_says_how_the_run_ended() -> None:
    recorder = _Recorder()
    machine = StateMachine(
        {State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.NEEDS_HUMAN)},
        emit=recorder,
    )

    await machine.run(_context())

    last = recorder.events[-1]

    assert last.state is State.NEEDS_HUMAN
    # NEEDS_HUMAN is not a failure, so it does not arrive as one.
    assert last.type is EventType.COMPLETED


async def test_starting_in_a_terminal_state_does_nothing() -> None:
    machine = StateMachine({}, start=State.COMPLETED)

    result = await machine.run(_context())

    assert result.final_state is State.COMPLETED
    assert result.transitions == 0


def test_the_terminal_states_are_the_three_from_the_planning_document() -> None:
    assert {State.COMPLETED, State.NEEDS_HUMAN, State.FAILED} == TERMINAL_STATES
