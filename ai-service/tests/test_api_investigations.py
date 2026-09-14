"""The ``/investigations`` surface, with the agent and the backend replaced by stubs.

Two halves. The endpoints are checked against a service that schedules nothing, because what an
endpoint owes its caller is a 202 and a record, not a finished investigation. The service itself
is then driven directly, with the machine faked, because the interesting behaviour is what
happens around the run: the task being held, the callback being fed, and a dependency that will
not build ending as a failed investigation rather than as a task that vanishes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agents.context import InvestigationContext, RootCause
from agents.events import CallbackEmitter
from agents.postmortem import PostmortemDraft, PostmortemWriter
from agents.state_machine import AgentEvent, EventType, RunResult
from agents.states import State
from app.api import investigations as investigations_api
from app.api.investigations import (
    InvestigationService,
    StartInvestigationRequest,
)
from app.config import settings
from app.main import app
from rag.memory import Remembered
from tests.support import ScriptedProvider

START = {
    "investigation_id": "11111111-1111-1111-1111-111111111111",
    "incident_id": "INC-00142",
    "query": "orders is timing out, find out why",
    "service_hint": "orders",
}


class FakeMachine:
    """A machine that emits two events and concludes, in no time at all."""

    def __init__(self, emit: Any) -> None:
        self._emit = emit

    async def run(self, ctx: InvestigationContext) -> RunResult:
        await self._emit(
            AgentEvent(type=EventType.STEP_COMPLETED, state=State.PLAN, message="planned")
        )
        ctx.root_cause = RootCause(
            title="The connection pool was reduced to 20",
            explanation="200 of 200 connections are in use.",
            category="DB_CONNECTION_POOL_EXHAUSTION",
            confidence=0.79,
        )
        await self._emit(
            AgentEvent(
                type=EventType.COMPLETED,
                state=State.COMPLETED,
                message="investigation ended in COMPLETED",
                payload={"transitions": 2},
            )
        )

        return RunResult(final_state=State.COMPLETED, transitions=2, duration_ms=12)


class SilentService(InvestigationService):
    """Schedules a task that does nothing, so an endpoint test is not a test of the agent."""

    async def _run(self, record: Any) -> None:
        return None


@pytest.fixture
def client():
    service = SilentService(settings())
    app.dependency_overrides[investigations_api.get_investigations] = lambda: service

    with TestClient(app) as test_client:
        test_client.service = service  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()


# -------------------------------------------------------------------- endpoints ----


def test_starting_an_investigation_is_accepted_not_answered(client: TestClient) -> None:
    """202: the question has been taken, and the answer arrives over the callback."""
    response = client.post("/investigations", json=START)

    assert response.status_code == 202
    body = response.json()
    assert body["investigation_id"] == START["investigation_id"]
    assert body["status"] == "running"


def test_starting_the_same_investigation_twice_is_a_conflict(client: TestClient) -> None:
    """A retried start would otherwise interleave two runs into one timeline."""
    client.post("/investigations", json=START)
    response = client.post("/investigations", json=START)

    assert response.status_code == 409
    assert START["investigation_id"] in response.json()["detail"]


def test_the_budget_knobs_reach_the_context(client: TestClient) -> None:
    client.post("/investigations", json={**START, "window_minutes": 180, "tool_budget": 40})

    ctx = client.service.get(START["investigation_id"]).ctx  # type: ignore[attr-defined]

    assert ctx.window_minutes == 180
    assert ctx.tool_budget == 40
    # Not given, so the context's own default rather than a second copy of it in the request model.
    assert ctx.max_iterations == 3


def test_omitted_knobs_leave_the_defaults_alone(client: TestClient) -> None:
    client.post("/investigations", json=START)

    ctx = client.service.get(START["investigation_id"]).ctx  # type: ignore[attr-defined]

    assert (ctx.window_minutes, ctx.tool_budget, ctx.max_iterations) == (30, 25, 3)


def test_a_status_for_an_investigation_this_process_never_ran_is_a_404(client: TestClient) -> None:
    assert client.get("/investigations/does-not-exist").status_code == 404


def test_the_status_reports_what_the_run_has_emitted(client: TestClient) -> None:
    client.post("/investigations", json=START)
    body = client.get(f"/investigations/{START['investigation_id']}").json()

    assert body["status"] == "running"
    assert body["incident_code"] == "INC-00142"
    assert body["events"] == []
    assert body["result"] is None
    # No callback URL was given, so there is nothing to report about delivery.
    assert body["callback"] is None


def test_the_started_runs_are_listed(client: TestClient) -> None:
    client.post("/investigations", json=START)
    client.post("/investigations", json={**START, "investigation_id": "22222222"})

    listed = {row["investigation_id"] for row in client.get("/investigations").json()}

    assert listed == {START["investigation_id"], "22222222"}


def test_a_query_is_required(client: TestClient) -> None:
    response = client.post("/investigations", json={**START, "query": ""})

    assert response.status_code == 422


# -------------------------------------------------------------------- the service ----


def request(**overrides: Any) -> StartInvestigationRequest:
    return StartInvestigationRequest(**{**START, **overrides})


def stub_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP client, the retriever and the write-up.

    The first two a faked machine never touches. The third it does: since Phase 9 a run that
    reached a conclusion is written up and ingested after it ends, which is a model call and a
    knowledge base these tests are not about. `test_a_finished_run_is_written_up` covers it.
    """

    async def none(self: InvestigationService) -> None:
        return None

    async def no_postmortem(self: InvestigationService, record: Any) -> None:
        return None

    stub_dependencies_without_postmortem(monkeypatch)
    monkeypatch.setattr(InvestigationService, "_remember", no_postmortem)


def stub_dependencies_without_postmortem(monkeypatch: pytest.MonkeyPatch) -> None:
    """The MCP client and the retriever only, for the two tests that are about the write-up."""

    async def none(self: InvestigationService) -> None:
        return None

    monkeypatch.setattr(InvestigationService, "_mcp_client", none)
    monkeypatch.setattr(InvestigationService, "_retriever", none)
    monkeypatch.setattr(InvestigationService, "_similarity", none)


async def test_a_run_records_its_result_when_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_dependencies(monkeypatch)
    monkeypatch.setattr(
        investigations_api, "build_machine", lambda **kwargs: FakeMachine(kwargs["emit"])
    )

    service = InvestigationService(settings())
    record = service.start(request())
    await record.task

    assert record.status == "completed"
    assert record.final_state is State.COMPLETED
    assert record.duration_ms == 12
    assert record.result["root_cause"]["category"] == "DB_CONNECTION_POOL_EXHAUSTION"
    assert [event["state"] for event in record.recorder.events] == ["PLAN", "COMPLETED"]


async def test_a_dependency_that_will_not_build_ends_as_a_failed_investigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not a task that disappears: the backend would hold a spinner open for ever."""
    stub_dependencies(monkeypatch)

    def explode(**kwargs: Any) -> None:
        raise RuntimeError("Ollama is not running")

    monkeypatch.setattr(investigations_api, "build_machine", explode)

    service = InvestigationService(settings())
    record = service.start(request())
    await record.task

    assert record.status == "failed"
    assert record.final_state is State.FAILED
    assert "Ollama is not running" in record.failure_reason
    # The backend hears about it, and hears that it never got as far as a state.
    assert record.recorder.events[-1]["type"] == "failed"
    assert record.recorder.events[-1]["payload"]["phase"] == "startup"


async def test_the_callback_receives_every_event_of_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_dependencies(monkeypatch)
    monkeypatch.setattr(
        investigations_api, "build_machine", lambda **kwargs: FakeMachine(kwargs["emit"])
    )

    posted: list[dict[str, Any]] = []

    def handle(http_request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(http_request.content))

        return httpx.Response(202)

    service = InvestigationService(settings())
    started = request(
        callback_url="http://backend:8080/internal/investigations/i/events",
        callback_token="token-123",
    )
    record = service.start(started)

    # The emitter is created by `start`; giving it a transport now is what keeps this test off
    # the network without making the service take a client it would otherwise never need.
    assert isinstance(record.emitter, CallbackEmitter)
    record.emitter._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))  # noqa: SLF001
    record.emitter._owns_client = True  # noqa: SLF001

    await record.task

    assert [event["type"] for event in posted] == ["step_completed", "completed"]
    assert posted[-1]["payload"]["result"]["status"] == "completed"
    assert record.emitter.delivered == 2


async def test_a_finished_run_is_written_up_and_remembered(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Phase 9's wiring: the conclusion becomes a document the next investigation can find."""
    stub_dependencies_without_postmortem(monkeypatch)
    monkeypatch.setattr(
        investigations_api, "build_machine", lambda **kwargs: FakeMachine(kwargs["emit"])
    )

    draft = PostmortemDraft(
        summary="Orders timed out after the connection pool was cut to 20.",
        what_we_saw="p95 latency reached 4.2 s and the pool was saturated.",
        lessons=["A pool size is a capacity limit."],
    )
    remembered: list[Any] = []

    class FakeMemory:
        async def remember(self, postmortem: Any) -> Any:
            remembered.append(postmortem)

            return Remembered(
                incident_code=postmortem.incident_code,
                path=tmp_path / postmortem.filename,
                written=True,
                indexed=True,
                chunks=3,
                status="ingested",
            )

    async def memory(self: InvestigationService) -> Any:
        return FakeMemory()

    monkeypatch.setattr(InvestigationService, "_memory", memory)

    service = InvestigationService(settings())
    service._writer = PostmortemWriter(ScriptedProvider([draft.model_dump_json()]))
    record = service.start(request())
    await record.task

    assert record.status == "completed"
    assert record.postmortem == {
        "written": True,
        "indexed": True,
        "path": str(tmp_path / remembered[0].filename),
        "status": "ingested",
        "error": None,
        "title": remembered[0].title,
    }

    # The document carries the run's conclusion, and the timeline the run actually emitted.
    assert "The connection pool was reduced to 20" in remembered[0].markdown
    assert "| PLAN | planned |" in remembered[0].markdown


async def test_a_run_that_reached_no_conclusion_is_not_written_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEEDS_HUMAN leaves no precedent behind, and costs no model call trying to write one."""
    stub_dependencies_without_postmortem(monkeypatch)

    class Inconclusive(FakeMachine):
        async def run(self, ctx: InvestigationContext) -> RunResult:
            return RunResult(final_state=State.NEEDS_HUMAN, transitions=1, duration_ms=9)

    monkeypatch.setattr(
        investigations_api, "build_machine", lambda **kwargs: Inconclusive(kwargs["emit"])
    )

    provider = ScriptedProvider([])
    service = InvestigationService(settings())
    service._writer = PostmortemWriter(provider)
    record = service.start(request())
    await record.task

    assert record.postmortem is None
    assert provider.calls == 0
