"""The HTTP surface, with the provider and the backend replaced by stubs.

Covers the schema translation and the failure paths that are awkward to reproduce against a real
model: a runtime that is down, and a model that never produces valid JSON.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import llm as llm_api
from app.main import app
from llm.base import LlmCompletion, LlmMessage, LlmOptions, LlmUnavailableError, LocalLlmProvider
from tests.support import RecordingReporter

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string", "description": "The failing service."},
        "severity": {"type": "string", "enum": ["low", "high"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"},
    },
    "required": ["service", "severity"],
}


class StubProvider(LocalLlmProvider):
    def __init__(self, text: str = '{"service": "orders", "severity": "high"}') -> None:
        self._text = text
        self.available = True

    @property
    def model(self) -> str:
        return "stub-model"

    async def complete(
        self, messages: list[LlmMessage], options: LlmOptions | None = None
    ) -> LlmCompletion:
        return LlmCompletion(
            text=self._text,
            model="stub-model",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=42,
        )

    async def is_available(self) -> bool:
        return self.available


class UnavailableProvider(StubProvider):
    async def complete(
        self, messages: list[LlmMessage], options: LlmOptions | None = None
    ) -> LlmCompletion:
        raise LlmUnavailableError("Ollama is not running.")

    async def is_available(self) -> bool:
        return False


@pytest.fixture
def reporter() -> RecordingReporter:
    return RecordingReporter()


@pytest.fixture
def client(reporter: RecordingReporter):
    provider = StubProvider()
    app.dependency_overrides[llm_api.get_provider] = lambda: provider
    app.dependency_overrides[llm_api.get_reporter] = lambda: reporter

    with TestClient(app) as test_client:
        test_client.provider = provider  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()


# ------------------------------------------------------------------------- meta ----


def test_service_health_does_not_depend_on_the_model_runtime(client: TestClient) -> None:
    """The service being up and Ollama being up are different questions."""
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "up"


def test_llm_health_reports_a_down_runtime_as_200_with_false(reporter: RecordingReporter) -> None:
    app.dependency_overrides[llm_api.get_provider] = lambda: UnavailableProvider()
    app.dependency_overrides[llm_api.get_reporter] = lambda: reporter

    try:
        with TestClient(app) as test_client:
            response = test_client.get("/llm/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "ollama pull" in body["detail"]


def test_prompts_are_listed(client: TestClient) -> None:
    ids = {p["id"] for p in client.get("/prompts").json()}

    assert "structured_extraction.v1" in ids


# ------------------------------------------------------------------- structured ----


def test_structured_returns_schema_conforming_json(client: TestClient) -> None:
    response = client.post(
        "/llm/structured",
        json={"prompt": "Orders is throwing 500s.", "json_schema": SCHEMA},
    )

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["data"]["service"] == "orders"
    assert body["data"]["severity"] == "high"
    # Optional fields are present and null rather than absent, so callers can read them directly.
    assert body["data"]["note"] is None


def test_usage_is_reported(client: TestClient) -> None:
    body = client.post(
        "/llm/structured",
        json={"prompt": "Orders is throwing 500s.", "json_schema": SCHEMA},
    ).json()

    assert body["usage"] == {
        "model": "stub-model",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "latency_ms": 42,
        "attempts": 1,
        "retries": 0,
    }


def test_a_successful_call_is_recorded(client: TestClient, reporter: RecordingReporter) -> None:
    client.post("/llm/structured", json={"prompt": "x", "json_schema": SCHEMA})

    assert len(reporter.records) == 1
    record = reporter.records[0]
    assert record.valid_json is True
    assert record.latency_ms == 42
    assert record.purpose.value == "reasoning"


def test_a_prompt_template_can_wrap_the_input(client: TestClient) -> None:
    response = client.post(
        "/llm/structured",
        json={
            "prompt": "Orders is throwing 500s.",
            "json_schema": SCHEMA,
            "prompt_name": "structured_extraction",
        },
    )

    assert response.status_code == 200, response.text


def test_an_unknown_prompt_is_404(client: TestClient) -> None:
    response = client.post(
        "/llm/structured",
        json={"prompt": "x", "json_schema": SCHEMA, "prompt_name": "nope"},
    )

    assert response.status_code == 404


# --------------------------------------------------------------------- failures ----


def test_a_down_runtime_is_503_not_422(reporter: RecordingReporter) -> None:
    """A caller has to be able to tell "start Ollama" apart from "your schema is wrong"."""
    app.dependency_overrides[llm_api.get_provider] = lambda: UnavailableProvider()
    app.dependency_overrides[llm_api.get_reporter] = lambda: reporter

    try:
        with TestClient(app) as test_client:
            response = test_client.post(
                "/llm/structured", json={"prompt": "x", "json_schema": SCHEMA}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    assert reporter.records == [], "an unreachable runtime is not a model prediction"


def test_output_that_never_validates_is_422_and_still_recorded(
    reporter: RecordingReporter,
) -> None:
    """A call that never produced valid JSON is the row the success rate is measured from."""
    app.dependency_overrides[llm_api.get_provider] = lambda: StubProvider("not json")
    app.dependency_overrides[llm_api.get_reporter] = lambda: reporter

    try:
        with TestClient(app) as test_client:
            response = test_client.post(
                "/llm/structured",
                json={"prompt": "x", "json_schema": SCHEMA, "max_attempts": 2},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert len(reporter.records) == 1
    assert reporter.records[0].valid_json is False


def test_a_non_object_schema_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/llm/structured",
        json={"prompt": "x", "json_schema": {"type": "string"}},
    )

    assert response.status_code == 422
    assert "object" in response.json()["detail"]


def test_an_empty_properties_map_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/llm/structured",
        json={"prompt": "x", "json_schema": {"type": "object", "properties": {}}},
    )

    assert response.status_code == 422
