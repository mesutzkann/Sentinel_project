"""The callback emitter: the envelope it builds, and what it does when the backend is not there.

The interesting tests here are the failure ones. Delivery working is one assertion; delivery
failing without taking the investigation down with it is the reason this module exists, and every
way it can fail — a dead connection, a 503, a rejected token — has a different correct answer.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agents.context import EvidenceSource, Hypothesis, RootCause
from agents.events import (
    CALLBACK_TOKEN_HEADER,
    CallbackEmitter,
    EventRecorder,
    envelope,
    fan_out,
)
from agents.state_machine import AgentEvent, EventType
from agents.states import State
from tests.support import context, pool_evidence


class FakeBackend:
    """An HTTP backend that answers from a script, and remembers what it was sent.

    ``script`` entries are status codes, or exceptions to raise instead of answering — which is
    what a backend that is not listening looks like from here.
    """

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.attempts: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.tokens: list[str | None] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        outcome = self.script.pop(0) if self.script else 200
        body = json.loads(request.content)
        self.attempts.append(body)
        self.tokens.append(request.headers.get(CALLBACK_TOKEN_HEADER))

        if isinstance(outcome, Exception):
            raise outcome

        if outcome < 400:
            self.received.append(body)

        return httpx.Response(outcome, text="no" if outcome >= 400 else "ok")

    @property
    def sequences(self) -> list[int]:
        return [event["sequence"] for event in self.received]


def step(state: State = State.COLLECT_LOGS, **payload: Any) -> AgentEvent:
    return AgentEvent(
        type=EventType.STEP_COMPLETED,
        state=state,
        message=f"{state} completed",
        payload=payload or None,
    )


async def emitter(backend: FakeBackend, **overrides: Any) -> CallbackEmitter:
    ctx = context()

    return CallbackEmitter(
        ctx,
        "http://backend:8080/internal/investigations/i/events",
        "token-123",
        client=backend.client(),
        # Zero, because what is being tested is how many attempts are made and not how long the
        # emitter is prepared to wait between them.
        backoff_seconds=0.0,
        **overrides,
    )


# -------------------------------------------------------------------- the envelope ----


def test_the_envelope_carries_the_fields_the_contract_names() -> None:
    wire = envelope(step(), investigation_id="abc", sequence=4, timestamp="2026-09-11T10:00:00Z")

    assert wire["investigation_id"] == "abc"
    assert wire["sequence"] == 4
    assert wire["type"] == "step_completed"
    assert wire["state"] == "COLLECT_LOGS"
    assert wire["timestamp"] == "2026-09-11T10:00:00Z"


def test_model_usage_is_lifted_out_of_the_payload() -> None:
    """The backend writes a model_predictions row from it, so it is a field and not a detail."""
    wire = envelope(
        step(
            State.GENERATE_HYPOTHESES,
            prompt_id="hypotheses.v1",
            model="qwen2.5:3b-instruct",
            prompt_tokens=1100,
            completion_tokens=210,
            llm_latency_ms=8400,
            llm_retries=1,
            round=1,
        ),
        investigation_id="abc",
        sequence=1,
    )

    assert wire["llm_usage"] == {
        "model": "qwen2.5:3b-instruct",
        "prompt_tokens": 1100,
        "completion_tokens": 210,
        "latency_ms": 8400,
        "retries": 1,
    }
    # The prompt identifies the step rather than the cost of the call, so it stays where the
    # step's own payload is.
    assert wire["payload"] == {"prompt_id": "hypotheses.v1", "round": 1}


def test_tool_calls_are_lifted_and_default_to_empty() -> None:
    calls = [{"server": "logs-mcp", "tool": "get_recent_errors", "success": True}]

    assert envelope(step(tool_calls=calls), investigation_id="a", sequence=1)["tool_calls"] == calls
    assert envelope(step(), investigation_id="a", sequence=1)["tool_calls"] == []
    assert envelope(step(), investigation_id="a", sequence=1)["llm_usage"] is None


# -------------------------------------------------------------------- delivery ----


async def test_events_are_numbered_from_one_and_carry_the_token() -> None:
    backend = FakeBackend()
    emit = await emitter(backend)

    await emit(step(State.UNDERSTAND_INCIDENT))
    await emit(step(State.PLAN))
    await emit(step(State.COLLECT_LOGS))

    assert backend.sequences == [1, 2, 3]
    assert backend.tokens == ["token-123", "token-123", "token-123"]
    assert emit.delivered == 3
    assert emit.pending == 0


async def test_a_transient_failure_is_retried_and_then_lands() -> None:
    backend = FakeBackend([503, 200])
    emit = await emitter(backend)

    await emit(step())

    assert len(backend.attempts) == 2
    assert emit.delivered == 1
    assert emit.pending == 0


async def test_a_rejected_token_is_not_retried() -> None:
    """401 is 401 next time too. Retrying it spends the investigation's time to learn nothing."""
    backend = FakeBackend([401])
    emit = await emitter(backend)

    await emit(step())

    assert len(backend.attempts) == 1
    assert emit.dropped == 1
    assert emit.pending == 0


async def test_an_unreachable_backend_does_not_raise_and_the_event_is_held() -> None:
    down = httpx.ConnectError("connection refused")
    backend = FakeBackend([down, down, down])
    emit = await emitter(backend, max_attempts=3)

    await emit(step())

    assert len(backend.attempts) == 3
    assert emit.pending == 1
    assert emit.delivered == 0


async def test_a_held_event_is_delivered_before_the_one_that_follows_it() -> None:
    """Ordering is the point of the buffer: a timeline out of order is a timeline misread."""
    backend = FakeBackend([httpx.ConnectError("down")])
    emit = await emitter(backend, max_attempts=1)

    await emit(step(State.PLAN))
    assert emit.pending == 1

    await emit(step(State.COLLECT_LOGS))

    assert backend.sequences == [1, 2]
    assert [event["state"] for event in backend.received] == ["PLAN", "COLLECT_LOGS"]
    assert emit.pending == 0


async def test_a_lost_event_still_consumes_its_sequence_number() -> None:
    """A gap is how the backend knows something was lost. Renumbering would hide it."""
    backend = FakeBackend([401, 200])
    emit = await emitter(backend, max_attempts=1)

    await emit(step(State.PLAN))
    await emit(step(State.COLLECT_LOGS))

    assert backend.sequences == [2]
    assert emit.sequence == 2


async def test_the_buffer_drops_the_oldest_when_it_is_full() -> None:
    backend = FakeBackend([httpx.ConnectError("down")] * 10)
    emit = await emitter(backend, max_attempts=1, buffer_limit=2)

    for state in (State.PLAN, State.COLLECT_LOGS, State.COLLECT_METRICS):
        await emit(step(state))

    assert emit.pending == 2
    assert emit.dropped == 1


# -------------------------------------------------------------------- the final payload ----


async def test_the_terminal_event_carries_the_whole_investigation() -> None:
    """ADR-0002 accepts losing a step event only because this one can rebuild every row."""
    backend = FakeBackend()
    ctx = context()
    ctx.evidence.extend(pool_evidence())
    ctx.hypotheses.append(
        Hypothesis(title="The pool is exhausted", score=0.8, rank=1, selected=True)
    )
    ctx.root_cause = RootCause(
        title="The connection pool was reduced to 20",
        explanation="200 of 200 connections are in use.",
        category="DB_CONNECTION_POOL_EXHAUSTION",
        supporting_evidence=[0, 1],
        confidence=0.79,
    )
    ctx.record_llm(prompt_tokens=1000, completion_tokens=200, calls=2)

    emit = CallbackEmitter(ctx, "http://backend/events", client=backend.client())
    await emit(
        AgentEvent(
            type=EventType.COMPLETED,
            state=State.COMPLETED,
            message="investigation ended in COMPLETED",
            payload={"transitions": 14},
        )
    )

    result = backend.received[-1]["payload"]["result"]

    assert result["status"] == "completed"
    assert result["needs_human"] is False
    assert result["transitions"] == 14
    assert result["root_cause"]["category"] == "DB_CONNECTION_POOL_EXHAUSTION"
    assert [item["source"] for item in result["evidence"]] == [
        EvidenceSource.DATABASE,
        EvidenceSource.LOGS,
        EvidenceSource.DATABASE,
    ]
    assert result["usage"]["llm_calls"] == 2
    assert result["hypotheses"][0]["selected"] is True


async def test_a_run_that_stopped_for_a_human_completed_rather_than_failed() -> None:
    """There is no `needs_human` status in the backend's vocabulary, and it is not a failure."""
    backend = FakeBackend()
    ctx = context()
    emit = CallbackEmitter(ctx, "http://backend/events", client=backend.client())

    await emit(
        AgentEvent(
            type=EventType.COMPLETED,
            state=State.NEEDS_HUMAN,
            message="the critic rejected the conclusion twice",
            payload={"transitions": 9},
        )
    )

    result = backend.received[-1]["payload"]["result"]

    assert result["status"] == "completed"
    assert result["needs_human"] is True
    assert result["final_state"] == "NEEDS_HUMAN"


async def test_the_final_event_is_retried_harder_than_a_step() -> None:
    backend = FakeBackend([httpx.ConnectError("down")] * 4)
    ctx = context()
    emit = CallbackEmitter(
        ctx,
        "http://backend/events",
        client=backend.client(),
        backoff_seconds=0.0,
        max_attempts=2,
    )

    await emit(AgentEvent(type=EventType.FAILED, state=State.FAILED, message="node raised"))

    assert len(backend.attempts) == 5
    assert backend.received[-1]["payload"]["result"]["status"] == "failed"
    assert backend.received[-1]["payload"]["result"]["failure_reason"] == "node raised"


# -------------------------------------------------------------------- recording ----


async def test_the_recorder_numbers_the_same_events_the_same_way() -> None:
    backend = FakeBackend()
    emit_callback = await emitter(backend)
    recorder = EventRecorder(context().investigation_id)
    emit = fan_out(recorder, emit_callback)

    await emit(step(State.PLAN))
    await emit(step(State.COLLECT_LOGS))

    assert [event["sequence"] for event in recorder.events] == backend.sequences
    assert [event["state"] for event in recorder.events] == ["PLAN", "COLLECT_LOGS"]


async def test_fan_out_feeds_every_emitter() -> None:
    seen: list[list[str]] = [[], []]

    async def first(event: AgentEvent) -> None:
        seen[0].append(event.state)

    async def second(event: AgentEvent) -> None:
        seen[1].append(event.state)

    await fan_out(first, second)(step(State.PLAN))

    assert seen == [["PLAN"], ["PLAN"]]


@pytest.mark.parametrize("status_code", [500, 502, 503, 429, 408])
async def test_transient_statuses_are_retried(status_code: int) -> None:
    backend = FakeBackend([status_code, 200])
    emit = await emitter(backend)

    await emit(step())

    assert len(backend.attempts) == 2


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 422])
async def test_permanent_statuses_are_not(status_code: int) -> None:
    backend = FakeBackend([status_code, 200])
    emit = await emitter(backend)

    await emit(step())

    assert len(backend.attempts) == 1
