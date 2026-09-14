"""The ``/models`` surface the Models page reads.

Two properties matter more than the shape of the JSON. The page must report what has actually
been measured on this checkout rather than a table somebody typed, so a missing benchmark file
has to come back as `null` and not as numbers. And it must report what is actually *there*: a
model that is configured and absent is the state somebody opens this page to diagnose.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import models as models_api
from app.main import app
from routing.base import Router
from routing.plans import decide
from routing.rule_router import RuleBasedRouter
from routing.schema import Intent, RouteDecision

REPORT: dict[str, Any] = {
    "generated_at": "2026-09-14T12:00:00+00:00",
    "split": "test",
    "prompt": "v2",
    "examples": 333,
    "scores": [
        {
            "router": "rule",
            "examples": 333,
            "intent_accuracy": 0.354,
            "service_accuracy": 0.976,
            "tool_precision": 0.409,
            "tool_recall": 0.410,
            "tool_f1": 0.410,
            "invalid_json": 0.0,
            "latency_p50": 0,
            "latency_p95": 0,
            "per_intent": {"LOG_QUERY": 0.39},
            "per_language": {"en": 0.09, "tr": 0.29, "mixed": 0.67},
            "confusions": [["LOG_QUERY", "GENERAL_QUESTION", 19]],
        },
        {
            "router": "sentinel-router [v2]",
            "examples": 333,
            "intent_accuracy": 0.871,
            "service_accuracy": 0.964,
            "tool_precision": 0.670,
            "tool_recall": 0.791,
            "tool_f1": 0.726,
            "invalid_json": 0.0,
            "latency_p50": 1241,
            "latency_p95": 2295,
            "per_intent": {"LOG_QUERY": 1.0},
            "per_language": {"en": 0.98, "tr": 0.77, "mixed": 0.87},
            "confusions": [["TRACE_QUERY", "LOG_QUERY", 10]],
        },
    ],
    # The per-question detail the file also carries, which this endpoint must not publish.
    "outcomes": {"rule": [{"example_id": "R00001", "query": "..."}]},
}


class StubRouter(Router):
    """A router that answers instantly, so the endpoint can be tested without a model."""

    def __init__(self, intent: Intent, name: str = "fine_tuned") -> None:
        self._intent = intent
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def route(self, query: str, *, service_hint: str | None = None) -> RouteDecision:
        return decide(self._intent, service_hint or "payments")


@pytest.fixture
def client(monkeypatch, tmp_path):
    """No benchmark file by default: the state of a fresh checkout, and no Ollama needed."""
    monkeypatch.setattr(models_api, "BENCHMARK_REPORT", tmp_path / "absent.json")

    async def present(name: str, config) -> bool:  # noqa: ANN001 - the settings object
        return True

    monkeypatch.setattr(models_api, "probe", present)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def test_the_configured_models_are_listed_with_their_purpose(client: TestClient) -> None:
    body = client.get("/models").json()
    purposes = [model["purpose"] for model in body["models"]]

    assert purposes == ["reasoning", "routing", "embedding"]
    assert body["router"]["active"] == "fine_tuned"
    assert body["router"]["fallback"] == "rule"


def test_an_absent_router_model_is_told_to_be_built_rather_than_pulled() -> None:
    """`ollama pull sentinel-router` sends somebody looking for a model that does not exist."""
    detail = models_api._detail("routing", "sentinel-router", available=False)

    assert "ollama create" in detail
    assert "pull" not in detail
    assert "ollama pull" in models_api._detail("reasoning", "qwen2.5:3b-instruct", available=False)


def test_no_benchmark_file_reports_nothing_rather_than_a_stale_table(client: TestClient) -> None:
    assert client.get("/models").json()["benchmark"] is None


def test_a_benchmark_is_published_with_the_run_that_produced_it(tmp_path) -> None:
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(REPORT), encoding="utf-8")

    benchmark = models_api.read_benchmark(path)

    assert benchmark is not None
    assert benchmark.split == "test"
    assert benchmark.prompt == "v2"
    assert benchmark.generated_at == "2026-09-14T12:00:00+00:00"
    assert [row.router for row in benchmark.rows] == ["rule", "sentinel-router [v2]"]
    assert benchmark.rows[1].intent_accuracy == 0.871
    assert benchmark.rows[1].per_language["tr"] == 0.77


def test_an_unreadable_benchmark_is_not_an_error(tmp_path) -> None:
    """One corrupt file must not take the page down; "nothing measured" is a true answer."""
    path = tmp_path / "benchmark.json"
    path.write_text("{ this is not json", encoding="utf-8")

    assert models_api.read_benchmark(path) is None


def test_the_per_question_outcomes_are_not_published(tmp_path) -> None:
    """The report carries every question and answer; the page needs the scores, not the corpus."""
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(REPORT), encoding="utf-8")

    published = models_api.read_benchmark(path)

    assert published is not None
    assert "outcomes" not in published.model_dump()


def test_routing_a_question_answers_with_both_routers(client: TestClient) -> None:
    app.dependency_overrides[models_api.get_router] = lambda: StubRouter(Intent.LOG_QUERY)

    body = client.post("/models/route", json={"query": "show me the payments logs"}).json()

    assert [answer["router"] for answer in body["answers"]] == ["fine_tuned", "rule"]
    assert body["answers"][0]["intent"] == "LOG_QUERY"
    assert body["answers"][0]["tools"] == decide(Intent.LOG_QUERY, "payments").tools
    assert body["agree"] is True


def test_a_disagreement_is_reported_as_one(client: TestClient) -> None:
    """The comparison is the point of the page, so the two answers standing apart is the signal."""
    app.dependency_overrides[models_api.get_router] = lambda: StubRouter(Intent.TRACE_QUERY)

    body = client.post("/models/route", json={"query": "show me the payments logs"}).json()

    assert body["agree"] is False
    assert body["answers"][0]["intent"] == "TRACE_QUERY"
    assert body["answers"][1]["intent"] == "LOG_QUERY"


def test_routing_by_keyword_alone_answers_once(client: TestClient) -> None:
    """With the model off there is no second column to draw, and no pretending there is."""
    app.dependency_overrides[models_api.get_router] = RuleBasedRouter

    body = client.post("/models/route", json={"query": "show me the payments logs"}).json()

    assert [answer["router"] for answer in body["answers"]] == ["rule"]
    assert body["agree"] is True


def test_a_blank_question_is_refused(client: TestClient) -> None:
    """Three spaces is not a question, and both routers would answer it with something."""
    assert client.post("/models/route", json={"query": ""}).status_code == 422
    assert client.post("/models/route", json={"query": "   "}).status_code == 422
