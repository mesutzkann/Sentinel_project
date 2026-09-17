"""Reporting model calls to the backend, which owns the table they live in.

The AI service owns the ``rag`` schema and the backend owns ``sentinel``; neither writes to the
other's. ``model_predictions`` is a ``sentinel`` table, so the service that makes the call
reports it over HTTP and the service that owns the table stores it.

**Who reports.** The ``/llm`` endpoint, the agent's four reasoning nodes (REASONING, and
VALIDATION for the critic) and the postmortem writer. Not the router: its calls happen before an
investigation has a plan and :meth:`routing.base.Router.route` is handed no investigation id, so
its rows would arrive unattached — and the router is the one call on the critical path whose
latency is itself the number being worked on. Its cost is measured by the Phase 8 benchmark
instead, which times the model rather than the report.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

from llm.structured import StructuredAttempt, StructuredResult

logger = logging.getLogger(__name__)

# How much of a model's *unusable* answer is kept on the row: enough to read what it said when a
# conclusion looks wrong, short enough that a model which rambled to the end of its context does
# not write a page per failed call. An answer that validated is stored whole — the column is
# `jsonb`, and a JSON object cut off at 4000 characters is no longer JSON.
OUTPUT_CHARS = 4000


class ModelPurpose(StrEnum):
    """Mirrors the backend's ``ModelPurpose``. Serialised snake_case, as the API expects."""

    ROUTING = "routing"
    REASONING = "reasoning"
    VALIDATION = "validation"
    POSTMORTEM = "postmortem"


@dataclass(frozen=True, slots=True)
class PredictionRecord:
    """One call, as the backend wants it."""

    model_name: str
    purpose: ModelPurpose
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    valid_json: bool
    output: str | None = None
    investigation_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "model_name": self.model_name,
            "purpose": self.purpose.value,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "valid_json": self.valid_json,
            "output": self.output,
        }

    @classmethod
    def from_attempts(
        cls,
        attempts: Sequence[StructuredAttempt],
        *,
        purpose: ModelPurpose,
        valid_json: bool,
        output: str | None,
        investigation_id: str | None = None,
    ) -> PredictionRecord:
        """One row for one structured call, repair attempts folded in.

        A call that needed two repairs cost three round trips and the caller waited for all of
        them, so the row carries their sum. It is deliberately not three rows: the thing the
        dashboard divides by is calls, and splitting them would make a model that retries look
        cheaper per call than one that gets it right first time.
        """
        if not attempts:
            raise ValueError("a prediction record needs at least one attempt")

        if output is not None and not valid_json:
            output = output[:OUTPUT_CHARS]

        return cls(
            model_name=attempts[-1].completion.model,
            purpose=purpose,
            prompt_tokens=sum(a.completion.prompt_tokens for a in attempts),
            completion_tokens=sum(a.completion.completion_tokens for a in attempts),
            latency_ms=sum(a.completion.latency_ms for a in attempts),
            valid_json=valid_json,
            output=output,
            investigation_id=investigation_id,
        )

    @classmethod
    def from_result(
        cls,
        result: StructuredResult[Any],
        *,
        purpose: ModelPurpose,
        investigation_id: str | None = None,
    ) -> PredictionRecord:
        """The row for a call that ended in a valid object.

        The parsed object is stored rather than the raw text: the text of the last attempt is the
        same thing plus whatever fence the model wrapped it in, and the failures are the rows
        where the exact bytes matter.
        """
        return cls.from_attempts(
            result.attempts,
            purpose=purpose,
            valid_json=True,
            output=result.value.model_dump_json(),
            investigation_id=investigation_id,
        )


class PredictionReporter:
    """Posts prediction records to the backend's internal API."""

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = internal_token
        self._timeout = timeout_seconds
        self._client = client

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    async def record(self, record: PredictionRecord) -> bool:
        """Report one call.

        Returns whether it was stored. Never raises: this is telemetry about a call that already
        happened, and losing the record must not turn a successful completion into a failed
        request. A failure is logged at error level rather than swallowed, so it shows up in Loki
        as a real problem instead of disappearing.
        """
        if not self.enabled:
            logger.debug("Prediction reporting is disabled; not recording %s", record.model_name)
            return False

        try:
            response = await self._post(record.to_payload())
        except httpx.HTTPError as exc:
            logger.error("Could not reach the backend to record a model prediction: %s", exc)
            return False

        if response.status_code >= 400:
            logger.error(
                "Backend rejected a model prediction with %d: %s",
                response.status_code,
                response.text[:300],
            )
            return False

        return True

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        url = f"{self._base_url}/internal/model-predictions"
        headers = {"X-Internal-Token": self._token}

        if self._client is not None:
            return await self._client.post(
                url, json=payload, headers=headers, timeout=self._timeout
            )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(url, json=payload, headers=headers)
