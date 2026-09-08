"""Reporting model calls to the backend, which owns the table they live in.

The AI service owns the ``rag`` schema and the backend owns ``sentinel``; neither writes to the
other's. ``model_predictions`` is a ``sentinel`` table, so the service that makes the call
reports it over HTTP and the service that owns the table stores it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)


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
