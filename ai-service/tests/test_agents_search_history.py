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
from rag.similarity import SimilarIncident


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


# ------------------------------------------------------------------ precedent ----


class _FakeSimilarity:
    """Answers with fixed precedents, and records what it was asked."""

    def __init__(self, found: list[SimilarIncident], raises: Exception | None = None) -> None:
        self._found = found
        self._raises = raises
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def find(self, summary, *, exclude=None, limit=3, service=None):  # type: ignore[no-untyped-def]
        self.calls.append((summary, exclude, service))

        if self._raises is not None:
            raise self._raises

        return self._found[:limit]


def _precedent(external_id: str, similarity: float, document_id: str = "") -> SimilarIncident:
    return SimilarIncident(
        external_id=external_id,
        title=f"{external_id} — orders timing out after a pool change",
        similarity=similarity,
        document_id=document_id or f"d-{external_id}",
        source_type="postmortem",
        service="orders",
        path=f"incidents/{external_id}.md",
        excerpt="The pool was cut from 200 to 20.",
    )


async def test_a_precedent_becomes_a_fact_the_investigation_can_cite() -> None:
    """Phase 9's done criterion: "this is 71% like INC-00001" reaches the evidence list."""
    node = SearchHistoryNode(
        _FakeRetriever([_chunk("runbook-pool", SourceType.RUNBOOK, rank=1)]),
        similarity=_FakeSimilarity([_precedent("INC-00001", 0.71)]),
    )
    ctx = _context()

    await node.run(ctx)

    precedent = [item for item in ctx.evidence if item.tool == "rag/similarity"]

    assert len(precedent) == 1
    assert precedent[0].summary.startswith("71% similar to INC-00001")
    assert precedent[0].source is EvidenceSource.HISTORICAL_INCIDENT
    assert precedent[0].raw["similarity"] == 0.71

    # Weighted like any other document: a precedent is a reason to look, never to conclude.
    assert precedent[0].weight <= WEIGHT_DOCUMENT


async def test_a_document_that_is_both_retrieved_and_alike_is_one_fact() -> None:
    """Two facts where there is one would be corroboration the confidence score counts twice."""
    node = SearchHistoryNode(
        _FakeRetriever(
            [_chunk("d-INC-00001", SourceType.POSTMORTEM, rank=1, external_id="INC-00001")]
        ),
        similarity=_FakeSimilarity([_precedent("INC-00001", 0.71, document_id="d-INC-00001")]),
    )
    ctx = _context()

    await node.run(ctx)

    assert len(ctx.evidence) == 1
    assert ctx.evidence[0].summary.startswith("71% similar — postmortem INC-00001")
    assert ctx.evidence[0].raw["similarity"] == 0.71


async def test_the_incident_is_excluded_from_its_own_precedents() -> None:
    similarity = _FakeSimilarity([])
    node = SearchHistoryNode(_FakeRetriever([]), similarity=similarity)

    await node.run(_context(incident_code="INC-00142"))

    _, exclude, service = similarity.calls[0]

    assert exclude == "INC-00142"
    assert service == "orders"


async def test_the_comparison_failing_does_not_end_the_run() -> None:
    """Same rule as retrieval: the knowledge base is not the investigation."""
    node = SearchHistoryNode(
        _FakeRetriever([_chunk("runbook-pool", SourceType.RUNBOOK, rank=1)]),
        similarity=_FakeSimilarity([], raises=RuntimeError("pgvector is not listening")),
    )
    ctx = _context()

    transition = await node.run(ctx)

    assert transition.next_state is not State.FAILED
    assert len(ctx.evidence) == 1
    assert any("similarity search failed" in note for note in ctx.notes)


async def test_without_a_comparison_the_node_behaves_as_it_did_before() -> None:
    """The parameter is optional: the reasoning benchmark builds this node without a store."""
    node = SearchHistoryNode(_FakeRetriever([_chunk("r", SourceType.RUNBOOK, rank=1)]))
    ctx = _context()

    await node.run(ctx)

    assert len(ctx.evidence) == 1
    assert ctx.evidence[0].tool.startswith("rag/")
