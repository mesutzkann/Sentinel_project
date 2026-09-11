"""Getting each step to the backend, and not losing the investigation when that fails.

The runner produces :class:`agents.state_machine.AgentEvent` objects and knows nothing about
HTTP. This module is the other half: it numbers them, stamps them, puts them in the envelope
docs/planning.md §3.2 specifies, and posts them to the ``callback_url`` the backend supplied when
it started the run.

[ADR-0006](../../docs/adr/0006-callback-resilience.md) is the argument for the behaviour here;
three things follow from it and are worth reading in the code below.

*Delivery never raises.* An investigation is minutes of model calls and tool calls, and a backend
that restarted halfway through must not cost that. Every failure path ends in a log line and a
buffered envelope, never in an exception reaching the node that emitted the event.

*Sequence numbers are assigned on emission, not on delivery.* They come from the run, so a gap
means an event was lost and the backend can say so. Numbering on delivery would renumber around
the loss and produce a timeline that looks complete and is not.

*Permanent and transient failures are told apart.* A rejected token is 401 on every retry, so
retrying it spends the investigation's time to reach the same answer; a 503 is worth three
attempts and then a place in the buffer. The line between them is drawn once, in
:func:`_is_transient`.

The terminal event carries the whole investigation (:func:`agents.payloads.final_payload`), which
is what makes a lost intermediate event survivable: the backend can still write every row, and
only the timeline is poorer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx

from agents.context import InvestigationContext
from agents.payloads import final_payload
from agents.state_machine import AgentEvent, Emitter, EventType

logger = logging.getLogger(__name__)

# The header the callback token travels in. Named for what it is rather than reusing
# X-Internal-Token: that one is a long-lived shared secret for the service-to-service API, and
# this is a token issued for one investigation. Two different lifetimes should not share a header
# name, or rotating one quietly widens the other.
CALLBACK_TOKEN_HEADER = "X-Callback-Token"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_TIMEOUT_SECONDS = 10.0

# Undelivered envelopes kept in memory. An investigation emits on the order of forty events, so
# this holds every event of several failed runs; it is a guard against a leak rather than a
# working limit. When it is exceeded the *oldest* go, because the newest carry the conclusion.
DEFAULT_BUFFER_LIMIT = 500

# The events that must not be lost — they carry the whole investigation — so they are retried
# further than a step event. Still bounded: a backend that has been down for a minute is not
# coming back inside this run.
TERMINAL_EVENT_TYPES = frozenset({EventType.COMPLETED, EventType.FAILED})
TERMINAL_MAX_ATTEMPTS = 5

# Lifted out of a step payload into the envelope's own fields, which is where the backend's
# `tool_calls` and `model_predictions` rows are written from. `prompt_id` deliberately stays in
# the payload: it describes the step, not what the call cost.
_USAGE_KEYS = {
    "model": "model",
    "prompt_tokens": "prompt_tokens",
    "completion_tokens": "completion_tokens",
    "llm_latency_ms": "latency_ms",
    "llm_retries": "retries",
}


class _Outcome(StrEnum):
    """Three ways a post can end."""

    DELIVERED = "delivered"
    TRANSIENT = "transient"
    PERMANENT = "permanent"


def envelope(
    event: AgentEvent,
    *,
    investigation_id: str,
    sequence: int,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """The wire form of one event, per docs/planning.md §3.2.

    ``tool_calls`` and ``llm_usage`` are pulled out of the payload rather than being separate
    arguments a node would have to remember to pass. A node describes what it did in one
    dictionary; which parts of that description become their own rows on the other side is this
    module's problem.
    """
    payload = dict(event.payload or {})
    tool_calls = payload.pop("tool_calls", None)
    usage = {wire: payload.pop(key) for key, wire in _USAGE_KEYS.items() if key in payload}

    return {
        "investigation_id": investigation_id,
        "sequence": sequence,
        "type": event.type.value,
        "state": event.state.value,
        "message": event.message,
        "payload": payload,
        "tool_calls": tool_calls or [],
        "llm_usage": usage or None,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }


class CallbackEmitter:
    """Posts one investigation's events to the backend, in order, without ever raising.

    One instance per investigation: it owns that run's sequence counter and its buffer, and
    holding the context is what lets the terminal event carry the final payload without the
    runner having to know that the terminal event is special.
    """

    def __init__(
        self,
        ctx: InvestigationContext,
        callback_url: str,
        callback_token: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        buffer_limit: int = DEFAULT_BUFFER_LIMIT,
    ) -> None:
        self._ctx = ctx
        self._url = callback_url
        self._token = callback_token
        self._client = client
        self._owns_client = client is None
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._timeout = timeout_seconds
        self._buffer_limit = buffer_limit

        self._sequence = 0
        self._pending: list[dict[str, Any]] = []
        self._started = time.perf_counter()

        self.delivered = 0
        self.dropped = 0

    @property
    def url(self) -> str:
        """Where events are being posted. Reported by the status endpoint."""
        return self._url

    @property
    def sequence(self) -> int:
        """The number of the last event emitted, delivered or not."""
        return self._sequence

    @property
    def pending(self) -> int:
        """Envelopes emitted and not yet accepted by the backend."""
        return len(self._pending)

    async def __call__(self, event: AgentEvent) -> None:
        """Emit one event. Satisfies the runner's ``Emitter`` signature."""
        self._sequence += 1
        terminal = event.type in TERMINAL_EVENT_TYPES
        wire = envelope(
            self._enrich(event) if terminal else event,
            investigation_id=self._ctx.investigation_id,
            sequence=self._sequence,
        )

        # Anything held from an earlier failure goes first, so the backend sees the run in the
        # order it happened. If the backend is still down this stops at the first failure rather
        # than hammering it once per buffered event.
        await self._flush()
        await self._deliver(
            wire,
            attempts=TERMINAL_MAX_ATTEMPTS if terminal else self._max_attempts,
        )

        if terminal:
            # One last go at whatever is still held. The run is over, so nothing else will
            # trigger a flush, and this is the buffered events' last chance to arrive.
            await self._flush()

    async def aclose(self) -> None:
        """Release the HTTP client, if this emitter made one.

        Reports what never arrived at warning level rather than silently: an investigation whose
        events did not reach the backend looks, from the frontend, exactly like one that never
        ran, and this service's own log is then the only place the truth exists.
        """
        if self._pending:
            logger.warning(
                "Investigation %s finished with %d undelivered event(s); the backend has an "
                "incomplete timeline for it",
                self._ctx.investigation_id,
                len(self._pending),
            )

        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _enrich(self, event: AgentEvent) -> AgentEvent:
        """Attach the whole investigation to the terminal event."""
        payload = dict(event.payload or {})
        duration_ms = int((time.perf_counter() - self._started) * 1000)

        result = final_payload(
            self._ctx,
            final_state=event.state,
            transitions=payload.get("transitions"),
            duration_ms=duration_ms,
            failure_reason=event.message if event.type is EventType.FAILED else None,
        )

        return AgentEvent(
            type=event.type,
            state=event.state,
            message=event.message,
            payload={**payload, "duration_ms": duration_ms, "result": result},
        )

    async def _flush(self) -> None:
        """Retry buffered envelopes oldest first, stopping at the first that still fails."""
        while self._pending:
            if not await self._retry_buffered(self._pending[0]):
                return

            self._pending.pop(0)

    async def _deliver(self, wire: dict[str, Any], *, attempts: int) -> None:
        """Post one envelope, retrying transient failures, then buffer it if it never lands."""
        for attempt in range(1, attempts + 1):
            outcome = await self._attempt(wire)

            if outcome is _Outcome.DELIVERED:
                self.delivered += 1

                return

            if outcome is _Outcome.PERMANENT:
                # Retrying produces the same rejection. Dropped rather than buffered, so the
                # buffer does not fill with envelopes that can never be accepted and push out
                # the ones that could.
                self.dropped += 1

                return

            if attempt < attempts:
                await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))

        self._buffer(wire)

    async def _attempt(self, wire: dict[str, Any]) -> _Outcome:
        try:
            response = await self._post(wire)
        except httpx.HTTPError as exc:
            logger.warning(
                "Callback for investigation %s event %d failed: %s",
                wire["investigation_id"],
                wire["sequence"],
                exc,
            )

            return _Outcome.TRANSIENT

        if response.status_code < 400:
            return _Outcome.DELIVERED

        logger.warning(
            "Backend answered %d to investigation %s event %d: %s",
            response.status_code,
            wire["investigation_id"],
            wire["sequence"],
            response.text[:300],
        )

        return _Outcome.TRANSIENT if _is_transient(response.status_code) else _Outcome.PERMANENT

    async def _retry_buffered(self, wire: dict[str, Any]) -> bool:
        """One attempt at a held envelope, and whether it can leave the buffer.

        A permanent rejection leaves the buffer too: it is not delivered, and keeping it would
        mean every later flush re-posts an envelope the backend has already refused.
        """
        outcome = await self._attempt(wire)

        if outcome is _Outcome.DELIVERED:
            self.delivered += 1

            return True

        if outcome is _Outcome.PERMANENT:
            self.dropped += 1

            return True

        return False

    async def _post(self, wire: dict[str, Any]) -> httpx.Response:
        headers = {CALLBACK_TOKEN_HEADER: self._token} if self._token else {}

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)

        return await self._client.post(self._url, json=wire, headers=headers)

    def _buffer(self, wire: dict[str, Any]) -> None:
        self._pending.append(wire)

        while len(self._pending) > self._buffer_limit:
            # Oldest first: the newest events carry the conclusion, and a timeline missing its
            # beginning is more use than one missing its end.
            self._pending.pop(0)
            self.dropped += 1


def _is_transient(status_code: int) -> bool:
    """Whether another attempt could plausibly succeed.

    Everything 5xx, plus the 4xx codes that mean "not now" rather than "not ever". A 401 or a 422
    is a disagreement about the token or about the shape, and neither changes between attempts.
    """
    return status_code >= 500 or status_code in {408, 425, 429}


class EventRecorder:
    """Keeps a run's events in memory, in the same envelope the backend is posted.

    This is what ``GET /investigations/{id}`` reads, and it exists so that the AI service can
    answer "what did this run do" without a database of its own. The envelope rather than the
    event, because the useful question when a timeline looks wrong is what the backend was
    *told*, not what the runner meant to say.

    Its sequence numbers are its own counter, not the emitter's. They agree because
    :func:`fan_out` calls both once per event in a fixed order — and if they ever did not, the
    recorded numbers would be the ones that are wrong, which is the harmless direction.
    """

    def __init__(self, investigation_id: str) -> None:
        self._investigation_id = investigation_id
        self._sequence = 0
        self.events: list[dict[str, Any]] = []

    async def __call__(self, event: AgentEvent) -> None:
        self._sequence += 1
        self.events.append(
            envelope(
                event,
                investigation_id=self._investigation_id,
                sequence=self._sequence,
            )
        )


def fan_out(*emitters: Emitter) -> Emitter:
    """One emitter that feeds several.

    The HTTP surface uses this to keep a run's events in memory for ``GET /investigations/{id}``
    while they also go to the backend. Sequential rather than gathered, so the in-memory record
    and the callback see the events in the same order — a status endpoint that disagreed with the
    timeline about what happened first would be worse than no status endpoint.
    """

    async def emit(event: AgentEvent) -> None:
        for emitter in emitters:
            await emitter(event)

    return emit
