"""The ``/investigations`` endpoints: start one, and ask what happened to it.

``POST /investigations`` is the contract in docs/planning.md §3.1 and it answers ``202 Accepted``
— the backend gets an acknowledgement in milliseconds and the investigation reports itself over
the callback for the next several minutes. That asymmetry is ADR-0002; the resilience the
callback needs to make it survivable is ADR-0006.

**The run is an asyncio task in this process, and it is held onto.** A task nobody keeps a
reference to can be garbage collected mid-await, which would end an investigation silently —
the one failure mode this service must not have, because from the frontend it is indistinguishable
from a backend that never started one.

**The dependencies are built inside the task, not in the request.** Filling the lexical index is
a table scan and discovering six MCP servers is six connections; doing either before answering
202 would make the acknowledgement wait on the slowest part of the stack, and a backend timing
out on ``POST /investigations`` while the investigation it asked for runs perfectly well is a
confusing way to fail.

**Nothing here is persisted.** ``GET /investigations/{id}`` reads memory and forgets on restart,
which is correct: the backend owns ``investigations`` and every other row, and a second store
here would be a second answer to what an investigation concluded. This endpoint is for watching
a run without the backend up, and for the demo script to poll.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from agents.build import build_machine
from agents.context import InvestigationContext
from agents.events import CallbackEmitter, Emitter, EventRecorder, fan_out
from agents.payloads import final_payload
from agents.postmortem import PostmortemWriter
from agents.state_machine import AgentEvent, EventType, RunResult
from agents.states import State
from app.api.mcp import get_client, get_registry
from app.api.rag import get_service as get_rag_service
from app.config import Settings, settings
from llm.ollama_provider import OllamaLlmProvider
from mcp_client.approval import approval_tokens
from mcp_client.client import McpClient
from mcp_client.policy import ApprovalVerifier, McpPolicy
from mcp_client.registry import McpToolRegistry
from rag.memory import IncidentMemory
from rag.retrievers import Retriever
from rag.similarity import IncidentSimilarity
from reporting.predictions import PredictionReporter
from routing.factory import build_router

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/investigations", tags=["investigations"])

# The retriever the agent searches history with. Phase 6 measured the four against 120 queries
# and this one won by every measure that matters here; naming it rather than reading a request
# field is deliberate, because which retriever the agent uses is an engineering decision and not
# a caller's option.
HISTORY_RETRIEVER = "hybrid_rerank"


@dataclass(slots=True)
class RunRecord:
    """One investigation this process is running or has run."""

    ctx: InvestigationContext
    started_at: datetime
    recorder: EventRecorder
    emitter: CallbackEmitter | None = None
    task: asyncio.Task[None] | None = None

    status: str = "running"
    final_state: State | None = None
    postmortem: dict[str, Any] | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    failure_reason: str | None = None

    # The final payload, built once when the run ends. Built from the context rather than copied
    # out of the terminal event so that a run whose callback failed still has a result to show.
    result: dict[str, Any] | None = None

    def finish(self, run: RunResult) -> None:
        self.status = "failed" if run.final_state is State.FAILED else "completed"
        self.final_state = run.final_state
        self.completed_at = datetime.now(UTC)
        self.duration_ms = run.duration_ms
        self.failure_reason = run.failure_reason
        self.result = final_payload(
            self.ctx,
            final_state=run.final_state,
            transitions=run.transitions,
            duration_ms=run.duration_ms,
            failure_reason=run.failure_reason,
        )

    def abort(self, reason: str) -> None:
        """End a run that never reached the machine, e.g. because a dependency would not build."""
        self.status = "failed"
        self.final_state = State.FAILED
        self.completed_at = datetime.now(UTC)
        self.failure_reason = reason
        self.result = final_payload(
            self.ctx,
            final_state=State.FAILED,
            failure_reason=reason,
        )


class InvestigationService:
    """Starts investigations and remembers what they did.

    One per process, like :class:`app.api.rag.RagService`, and for the same reason: the provider
    and the MCP client are connection-holding objects, and the run registry has to outlive the
    request that created it.
    """

    def __init__(self, config: Settings) -> None:
        self._config = config
        self._runs: dict[str, RunRecord] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._provider = OllamaLlmProvider(
            base_url=config.ollama_base_url,
            model=config.llm_model,
            timeout_seconds=config.llm_timeout_seconds,
        )
        self._router = build_router(config)

        # One reporter for the process. It holds no connection — every record opens its own
        # client — and it is disabled rather than absent when the backend token is not set, so
        # the agent runs the same way on a machine that has no backend to report to.
        self._reporter = PredictionReporter(
            base_url=config.backend_base_url,
            internal_token=config.internal_api_key,
        )

        # Its own writer rather than a node in the machine. The postmortem is written after the
        # investigation has ended and reported, so it cannot be a state the runner has to pass
        # through — and Phase 10 will want to write it again after a fix has been verified,
        # which is a second call to the same object rather than a second visit to a state.
        self._writer = PostmortemWriter(self._provider, reporter=self._reporter)

    def get(self, investigation_id: str) -> RunRecord | None:
        return self._runs.get(investigation_id)

    def all(self) -> list[RunRecord]:
        return list(self._runs.values())

    def running(self, investigation_id: str) -> bool:
        record = self._runs.get(investigation_id)

        return record is not None and record.status == "running"

    def start(self, request: StartInvestigationRequest) -> RunRecord:
        """Register the run and schedule it. Returns as soon as the task exists."""
        ctx = request.to_context()
        record = RunRecord(
            ctx=ctx,
            started_at=datetime.now(UTC),
            recorder=EventRecorder(ctx.investigation_id),
        )

        if request.callback_url:
            record.emitter = CallbackEmitter(
                ctx,
                request.callback_url,
                request.callback_token,
            )

        self._runs[ctx.investigation_id] = record
        task = asyncio.create_task(self._run(record))
        record.task = task

        # Held until it finishes. asyncio keeps only a weak reference to a running task, so a
        # task nobody else refers to can be collected mid-run.
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return record

    async def _run(self, record: RunRecord) -> None:
        emit = (
            fan_out(record.recorder, record.emitter)
            if record.emitter is not None
            else record.recorder
        )

        try:
            machine = build_machine(
                provider=self._provider,
                mcp_client=await self._mcp_client(),
                retriever=await self._retriever(),
                router=self._router,
                similarity=await self._similarity(),
                reporter=self._reporter,
                emit=emit,
            )
        except Exception as exc:  # noqa: BLE001 - reported as a failed investigation, not a crash
            logger.exception("investigation %s could not be started", record.ctx.investigation_id)
            await self._report_start_failure(record, emit, f"{type(exc).__name__}: {exc}")

            return

        try:
            record.finish(await machine.run(record.ctx))
        except Exception as exc:  # noqa: BLE001 - the runner catches node failures; this is the rest
            # Reaching here means the failure was outside a node — the emitter, or the runner
            # itself. The run still ends as an investigation rather than as a stack trace in a
            # log nobody is reading.
            logger.exception("investigation %s died", record.ctx.investigation_id)
            record.abort(f"{type(exc).__name__}: {exc}")
        finally:
            if record.emitter is not None:
                await record.emitter.aclose()

        logger.info(
            "investigation %s ended in %s after %s ms",
            record.ctx.investigation_id,
            record.final_state,
            record.duration_ms,
        )

        await self._remember(record)

    async def _remember(self, record: RunRecord) -> None:
        """Write the investigation up and put it in the knowledge base.

        After the emitter is closed and the run is recorded, deliberately: this is Phase 9's
        work, not the investigation's, and a model call for a document must not sit between the
        conclusion and the backend being told about it. The run has already ended either way —
        every failure here is reported into `record.postmortem` and logged, and none of them
        change what the investigation concluded.
        """
        if record.ctx.root_cause is None:
            return

        postmortem = await self._writer.write(
            record.ctx,
            duration_ms=record.duration_ms,
            events=record.recorder.events,
        )

        if postmortem is None:
            record.postmortem = {"written": False, "reason": "the model produced no write-up"}

            return

        remembered = await (await self._memory()).remember(postmortem)
        record.postmortem = {
            "written": remembered.written,
            "indexed": remembered.indexed,
            "path": str(remembered.path),
            "status": remembered.status,
            "error": remembered.error,
            "title": postmortem.title,
        }

        logger.info(
            "investigation %s remembered as %s (%s)",
            record.ctx.investigation_id,
            remembered.path.name,
            remembered.status,
        )

    async def _memory(self) -> IncidentMemory:
        rag = get_rag_service(self._config)

        return IncidentMemory(rag.pipeline, self._config.knowledge_base_dir)

    async def _report_start_failure(self, record: RunRecord, emit: Emitter, reason: str) -> None:
        """Tell the backend the run is over before it ever began.

        The machine never ran, so the runner never emitted anything, and without this the backend
        would hold an investigation in ``running`` for ever — which on the frontend is a spinner
        that never stops rather than an error somebody can act on.
        """
        record.abort(reason)

        await emit(
            AgentEvent(
                type=EventType.FAILED,
                state=State.FAILED,
                message=reason,
                payload={"phase": "startup"},
            )
        )

        if record.emitter is not None:
            await record.emitter.aclose()

    async def _mcp_client(self) -> McpClient:
        registry: McpToolRegistry = get_registry(self._config)

        if not registry.tools:
            # Discovery is cached for the process; the first investigation pays for it. A server
            # that is down stays absent until something asks again, which is what the collectors
            # turn into a note rather than a failure.
            await registry.discover()

        policy = McpPolicy(registry, ApprovalVerifier(tokens=approval_tokens(self._config)))

        return get_client(registry, policy, self._config)

    async def _similarity(self) -> IncidentSimilarity:
        """The precedent finder: the same embedding model and store the retriever uses.

        Its own object rather than a method on the retriever, because it asks a different
        question — "which past incident is this like", answered in cosine — and the retriever's
        fused scores are not similarities.
        """
        rag = get_rag_service(self._config)

        return IncidentSimilarity(rag.embeddings, rag.store)

    async def _retriever(self) -> Retriever:
        rag = get_rag_service(self._config)
        await rag.ensure_index()

        return rag.retrievers[HISTORY_RETRIEVER]


# ---------------------------------------------------------------------- contracts ----


class StartInvestigationRequest(BaseModel):
    """docs/planning.md §3.1, with the budget knobs the demo needs.

    ``incident_id`` is the incident *code* — ``INC-00142`` — and is named as the contract names
    it rather than as the context names it. The backend's own primary key never crosses this
    boundary: what the agent puts in a prompt and on a timeline is the code a human recognises.
    """

    investigation_id: str = Field(min_length=1, max_length=64)
    incident_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=2000)
    service_hint: str | None = Field(default=None, max_length=100)

    callback_url: str | None = Field(
        default=None,
        description=(
            "Where each step is posted. Omitted, the investigation runs and reports nowhere, "
            "which is what the demo script and a stack without the backend up both want."
        ),
    )
    callback_token: str | None = Field(
        default=None,
        description="Echoed back in the X-Callback-Token header of every event.",
    )

    window_minutes: int | None = Field(
        default=None,
        ge=1,
        le=1440,
        description=(
            "How far back the collectors look. The default suits an incident raised while it is "
            "happening; raise it for one raised hours later, or the collectors gather a healthy "
            "period and dilute what they are measuring."
        ),
    )
    tool_budget: int | None = Field(default=None, ge=1, le=200)
    max_iterations: int | None = Field(default=None, ge=1, le=10)

    def to_context(self) -> InvestigationContext:
        ctx = InvestigationContext(
            investigation_id=self.investigation_id,
            incident_code=self.incident_id,
            query=self.query,
            service_hint=self.service_hint,
        )

        # Assigned rather than passed, so that "not given" means the context's own default and
        # there is one place those defaults are written down.
        if self.window_minutes is not None:
            ctx.window_minutes = self.window_minutes

        if self.tool_budget is not None:
            ctx.tool_budget = self.tool_budget

        if self.max_iterations is not None:
            ctx.max_iterations = self.max_iterations

        return ctx


class StartResponse(BaseModel):
    investigation_id: str
    status: str
    accepted_at: datetime


class CallbackStatus(BaseModel):
    """What the emitter managed to deliver. Absent when no callback URL was given."""

    url: str
    delivered: int
    pending: int
    dropped: int


class InvestigationStatus(BaseModel):
    investigation_id: str
    incident_code: str
    query: str
    status: str
    final_state: State | None = None
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    failure_reason: str | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    callback: CallbackStatus | None = None


class InvestigationSummary(BaseModel):
    investigation_id: str
    incident_code: str
    status: str
    final_state: State | None = None
    started_at: datetime


_service: InvestigationService | None = None


def get_investigations(config: Annotated[Settings, Depends(settings)]) -> InvestigationService:
    global _service  # noqa: PLW0603 - process-wide, intentionally

    if _service is None:
        _service = InvestigationService(config)

    return _service


# ---------------------------------------------------------------------- endpoints ----


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=StartResponse)
async def start_investigation(
    request: StartInvestigationRequest,
    service: Annotated[InvestigationService, Depends(get_investigations)],
) -> StartResponse:
    """Accept an investigation and start running it.

    202 rather than 200: nothing has been investigated yet. A run takes tens of seconds to
    minutes, and the answer arrives over the callback.
    """
    if service.running(request.investigation_id):
        # Not idempotent-and-silent: the backend retrying a start it already made would otherwise
        # get a second run writing events under the same id, and the timeline would interleave
        # two investigations into one.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"investigation {request.investigation_id} is already running",
        )

    record = service.start(request)

    return StartResponse(
        investigation_id=record.ctx.investigation_id,
        status=record.status,
        accepted_at=record.started_at,
    )


@router.get("", response_model=list[InvestigationSummary])
async def list_investigations(
    service: Annotated[InvestigationService, Depends(get_investigations)],
) -> list[InvestigationSummary]:
    """What this process has run since it started."""
    return [
        InvestigationSummary(
            investigation_id=record.ctx.investigation_id,
            incident_code=record.ctx.incident_code,
            status=record.status,
            final_state=record.final_state,
            started_at=record.started_at,
        )
        for record in service.all()
    ]


@router.get("/{investigation_id}", response_model=InvestigationStatus)
async def investigation_status(
    investigation_id: str,
    service: Annotated[InvestigationService, Depends(get_investigations)],
) -> InvestigationStatus:
    """Everything this process knows about one run, including the events it emitted."""
    record = service.get(investigation_id)

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no investigation {investigation_id} in this process",
        )

    callback = None

    if record.emitter is not None:
        callback = CallbackStatus(
            url=record.emitter.url,
            delivered=record.emitter.delivered,
            pending=record.emitter.pending,
            dropped=record.emitter.dropped,
        )

    return InvestigationStatus(
        investigation_id=record.ctx.investigation_id,
        incident_code=record.ctx.incident_code,
        query=record.ctx.query,
        status=record.status,
        final_state=record.final_state,
        started_at=record.started_at,
        completed_at=record.completed_at,
        duration_ms=record.duration_ms,
        failure_reason=record.failure_reason,
        events=record.recorder.events,
        result=record.result,
        callback=callback,
    )
