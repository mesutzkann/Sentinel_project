"""The knowledge-base collector, and the ceiling on what a document is allowed to be worth."""

from __future__ import annotations

from typing import Any

from agents.context import EvidenceSource, InvestigationContext
from agents.nodes.collectors import WEIGHT_POSITIVE
from agents.nodes.search_history import (
    WEIGHT_DOCUMENT,
    WEIGHT_DOCUMENT_TAIL,
    SearchHistoryNode,
)
from agents.state_machine import EventType
from agents.states import State
from rag.documents import RetrievedChunk, SourceType, StoredChunk
from rag.retrievers import RetrievalResult


def _chunk(
    document_id: str,
    source_type: SourceType,
    *,
    rank: int,
    content: str = "the connection pool has been exhausted",
    external_id: str | None = None,
    chunk_index: int = 0,
) -> RetrievedChunk:
    stored = StoredChunk(
        chunk_id=f"{document_id}#{chunk_index}",
        document_id=document_id,
        content=content,
        chunk_index=chunk_index,
        title=document_id,
        source_type=source_type,
        service="orders",
        external_id=external_id,
        path=f"{document_id}.md",
        section="Symptom",
    )

    return RetrievedChunk(chunk=stored, score=1.0 / rank, rank=rank, retriever="hybrid_rerank")


class _FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk], name: str = "hybrid_rerank") -> None:
        self._chunks = chunks
        self._name = name
        self.calls: list[tuple[str, int, Any]] = []

    @property
    def name(self) -> str:
        return self._name

    async def retrieve(
        self, query: str, k: int = 5, filters: dict[str, Any] | None = None
    ) -> RetrievalResult:
        self.calls.append((query, k, filters))

        return RetrievalResult(
            query=query,
            retriever=self._name,
            chunks=self._chunks[:k],
            stage_latency_ms={"total": 1014},
            candidates={},
            filters=filters or {},
        )


class _BrokenRetriever:
    @property
    def name(self) -> str:
        return "hybrid_rerank"

    async def retrieve(
        self, query: str, k: int = 5, filters: dict[str, Any] | None = None
    ) -> RetrievalResult:
        raise ConnectionError("ollama is not reachable")


def _context(**kwargs: Any) -> InvestigationContext:
    defaults: dict[str, Any] = {
        "investigation_id": "11111111-1111-1111-1111-111111111111",
        "incident_code": "INC-00142",
        "query": "connections pinned at a round number and p99 at the timeout",
        "service_hint": "orders",
    }

    return InvestigationContext(**{**defaults, **kwargs})


async def test_it_spends_no_tool_budget() -> None:
    """The budget bounds the observability stack. Charging retrieval against it would let a run
    exhaust itself before ever asking whether this has happened before."""
    retriever = _FakeRetriever([_chunk("runbooks/pool", SourceType.RUNBOOK, rank=1)])
    ctx = _context(tool_budget=0)

    await SearchHistoryNode(retriever).run(ctx)  # type: ignore[arg-type]

    assert ctx.tool_calls_made == 0
    assert len(ctx.evidence) == 1


async def test_a_document_never_outweighs_something_measured() -> None:
    """A runbook describing a symptom is not the same kind of fact as observing it, and the
    confidence score must not be able to reach a recommendation from documents alone."""
    retriever = _FakeRetriever([_chunk("runbooks/pool", SourceType.RUNBOOK, rank=1)])
    ctx = _context()

    await SearchHistoryNode(retriever).run(ctx)  # type: ignore[arg-type]

    assert ctx.evidence[0].weight == WEIGHT_DOCUMENT
    assert WEIGHT_DOCUMENT < WEIGHT_POSITIVE


async def test_one_fact_per_document_rather_than_per_chunk() -> None:
    """Five chunks usually cover two or three documents, and an investigation wants the runbook
    and the postmortem rather than four chunks of one runbook."""
    retriever = _FakeRetriever(
        [
            _chunk("runbooks/pool", SourceType.RUNBOOK, rank=1, chunk_index=0),
            _chunk("runbooks/pool", SourceType.RUNBOOK, rank=2, chunk_index=1),
            _chunk("incidents/INC-00001", SourceType.POSTMORTEM, rank=3, external_id="INC-00001"),
        ]
    )
    ctx = _context()

    await SearchHistoryNode(retriever).run(ctx)  # type: ignore[arg-type]

    assert len(ctx.evidence) == 2
    assert [item.raw["document_id"] for item in ctx.evidence if item.raw] == [
        "runbooks/pool",
        "incidents/INC-00001",
    ]


async def test_the_ranking_survives_into_the_weights() -> None:
    """A ranked list is retrieval's only statement about its own confidence."""
    retriever = _FakeRetriever(
        [
            _chunk("runbooks/pool", SourceType.RUNBOOK, rank=1),
            _chunk("services/orders", SourceType.SERVICE_DOC, rank=2),
        ]
    )
    ctx = _context()

    await SearchHistoryNode(retriever).run(ctx)  # type: ignore[arg-type]

    assert [item.weight for item in ctx.evidence] == [WEIGHT_DOCUMENT, WEIGHT_DOCUMENT_TAIL]


async def test_past_incidents_are_a_different_source_from_runbooks() -> None:
    """One says this happened here before, the other says how the failure works. They
    corroborate independently, so the diversity bonus counts them separately."""
    retriever = _FakeRetriever(
        [
            _chunk("incidents/INC-00001", SourceType.INCIDENT, rank=1, external_id="INC-00001"),
            _chunk("runbooks/pool", SourceType.RUNBOOK, rank=2),
        ]
    )
    ctx = _context()

    await SearchHistoryNode(retriever).run(ctx)  # type: ignore[arg-type]

    assert ctx.sources_seen() == {
        EvidenceSource.HISTORICAL_INCIDENT,
        EvidenceSource.RAG_DOCUMENT,
    }


async def test_the_service_is_added_to_the_query_only_when_missing() -> None:
    retriever = _FakeRetriever([_chunk("runbooks/pool", SourceType.RUNBOOK, rank=1)])

    await SearchHistoryNode(retriever).run(_context())  # type: ignore[arg-type]
    await SearchHistoryNode(retriever).run(  # type: ignore[arg-type]
        _context(query="why is orders slow")
    )

    assert retriever.calls[0][0].endswith("(orders)")
    assert retriever.calls[1][0] == "why is orders slow"


async def test_nothing_is_filtered_by_service() -> None:
    """Filtering would hide the runbooks and architecture notes, which carry no service, and the
    neighbouring postmortem that is the other end of a cascade."""
    retriever = _FakeRetriever([_chunk("runbooks/pool", SourceType.RUNBOOK, rank=1)])

    await SearchHistoryNode(retriever).run(_context())  # type: ignore[arg-type]

    assert retriever.calls[0][2] is None


async def test_retrieval_failing_does_not_end_the_run() -> None:
    """The knowledge base being unavailable is a fact about the investigation, and must not stop
    a run that still has live signals to reason over."""
    ctx = _context()
    ctx.plan = [State.COLLECT_METRICS]

    transition = await SearchHistoryNode(_BrokenRetriever()).run(ctx)  # type: ignore[arg-type]

    assert transition.next_state is State.COLLECT_METRICS
    assert ctx.evidence == []
    assert any("knowledge base search failed" in note for note in ctx.notes)
    assert (transition.payload or {})["failed"] is True


async def test_matching_nothing_is_recorded_and_is_not_a_failure() -> None:
    ctx = _context()

    transition = await SearchHistoryNode(_FakeRetriever([])).run(ctx)  # type: ignore[arg-type]

    assert transition.next_state is State.GENERATE_HYPOTHESES
    assert (transition.payload or {})["documents"] == 0
    assert any("nothing in the knowledge base matched" in note for note in ctx.notes)


async def test_each_document_is_announced_with_its_path() -> None:
    """A citation an agent cannot resolve back to a file is an assertion."""
    retriever = _FakeRetriever([_chunk("runbooks/pool", SourceType.RUNBOOK, rank=1)])
    ctx = _context()

    transition = await SearchHistoryNode(retriever).run(ctx)  # type: ignore[arg-type]

    assert len(transition.events) == 1
    assert transition.events[0].type is EventType.EVIDENCE_FOUND
    assert (transition.events[0].payload or {})["path"] == "runbooks/pool.md"
