"""The ``/rag`` HTTP surface, with the store and the embedding runtime replaced by stubs.

What is covered here is the translation layer and the failure paths: an invalid filter, an
embedding runtime that is down, and an empty result. The retrieval quality itself is tested
where the retrieval is — through the index and the fusion, not through JSON.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import rag as rag_api
from app.main import app
from rag.context_builder import ContextBuilder
from rag.documents import RetrievedChunk, SourceType, StoredChunk
from rag.embeddings import EmbeddingUnavailableError
from rag.ingest import DocumentOutcome, IngestReport
from rag.retrievers import RetrievalResult
from rag.store import StoreStats

CHUNK = StoredChunk(
    chunk_id="chunk-1",
    document_id="doc-1",
    content="Connection pool exhaustion. Raise MaxPoolSize back to 200.",
    chunk_index=0,
    title="Runbook — connection pool exhaustion",
    source_type=SourceType.RUNBOOK,
    service="orders",
    external_id="RB-001",
    path="runbooks/pool.md",
    section="Runbook > Fix",
    metadata={"document_type": "runbook", "service": "orders"},
)


class _StubRetriever:
    def __init__(self, name: str, chunks: list[RetrievedChunk], error: Exception | None = None):
        self._name = name
        self._chunks = chunks
        self._error = error

    @property
    def name(self) -> str:
        return self._name

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        if self._error is not None:
            raise self._error

        return RetrievalResult(
            query=query,
            retriever=self._name,
            chunks=self._chunks[:k],
            stage_latency_ms={"total": 12},
            candidates={"bm25": ["chunk-1"], "vector": [], "fused": ["chunk-1"]},
            filters=filters or {},
        )


class _StubStore:
    def __init__(self) -> None:
        self.logged: list[dict[str, Any]] = []

    async def log_retrieval(self, **kwargs: Any) -> None:
        self.logged.append(kwargs)

    async def stats(self) -> StoreStats:
        return StoreStats(
            documents=28,
            chunks=94,
            documents_by_type={"runbook": 8, "postmortem": 10},
            documents_by_service={"orders": 7},
        )


class _StubEmbeddings:
    model = "bge-m3"

    async def is_available(self) -> bool:
        return True


class _StubPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, bool, bool]] = []
        self.error: Exception | None = None

    async def ingest_corpus(self, root: Path, force: bool = False, prune: bool = True):  # noqa: ANN201
        if self.error is not None:
            raise self.error

        self.calls.append((root, force, prune))

        return IngestReport(
            documents=[
                DocumentOutcome("runbooks/pool.md", "Pool", "ingested", chunks=3),
                DocumentOutcome("runbooks/leak.md", "Leak", "unchanged"),
                DocumentOutcome("gone.md", "Gone", "removed"),
                DocumentOutcome("bad.md", "bad.md", "failed", error="no front matter"),
            ],
            duration_ms=812,
        )


class _StubLexical:
    size = 94


class _StubReranker:
    model = "BAAI/bge-reranker-v2-m3"
    unavailable_reason = "sentence-transformers is not installed."

    async def is_available(self) -> bool:
        return False


class _StubService:
    def __init__(self, hits: list[RetrievedChunk] | None = None) -> None:
        chunks = hits if hits is not None else [
            RetrievedChunk(chunk=CHUNK, score=0.82, rank=1, retriever="hybrid")
        ]

        self.store = _StubStore()
        self.embeddings = _StubEmbeddings()
        self.lexical = _StubLexical()
        self.pipeline = _StubPipeline()
        self.reranker = _StubReranker()
        self.context_builder = ContextBuilder()
        self.retrievers = {
            "hybrid": _StubRetriever("hybrid", chunks),
            "bm25": _StubRetriever("bm25", chunks),
            "vector": _StubRetriever("vector", chunks),
            "hybrid_rerank": _StubRetriever("hybrid_rerank", chunks),
        }
        self.index_ready = False

    async def ensure_index(self) -> None:
        self.index_ready = True

    def invalidate_index(self) -> None:
        self.index_ready = True


@pytest.fixture
def service():  # noqa: ANN201
    stub = _StubService()
    app.dependency_overrides[rag_api.get_service] = lambda: stub

    yield stub

    app.dependency_overrides.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ------------------------------------------------------------------ assembly ----


def test_the_real_service_is_built_once_and_reused() -> None:
    """Constructs the real thing, with no overrides.

    Every other test here replaces the service, so nothing else would notice if assembling it
    raised — which is exactly what happened: the first version cached it with `lru_cache`, and
    `Settings` is not hashable, so every request to `/rag` was a 500 that no test saw.
    """
    from app.config import settings as real_settings

    rag_api._service = None  # noqa: SLF001 - the process-wide instance under test

    try:
        first = rag_api.get_service(real_settings())
        second = rag_api.get_service(real_settings())

        assert first is second
        assert set(first.retrievers) == {"bm25", "vector", "hybrid", "hybrid_rerank"}
    finally:
        rag_api._service = None  # noqa: SLF001


# -------------------------------------------------------------------- search ----


def test_a_search_returns_the_chunk_with_where_it_came_from(client, service) -> None:  # noqa: ANN001
    response = client.post("/rag/search", json={"query": "connection pool exhausted"})

    assert response.status_code == 200
    body = response.json()
    hit = body["results"][0]

    # The provenance fields are not decoration: an evidence panel that cannot say which runbook
    # a sentence came from is showing an assertion, not evidence.
    assert hit["title"] == "Runbook — connection pool exhaustion"
    assert hit["path"] == "runbooks/pool.md"
    assert hit["section"] == "Runbook > Fix"
    assert hit["source_type"] == "runbook"
    assert hit["rank"] == 1


def test_a_search_reports_both_candidate_lists(client, service) -> None:  # noqa: ANN001
    body = client.post("/rag/search", json={"query": "pool"}).json()

    # Which half proposed a chunk is the only thing that explains a surprising hybrid result.
    assert body["candidates"]["bm25"] == ["chunk-1"]
    assert body["candidates"]["fused"] == ["chunk-1"]


def test_the_retriever_can_be_chosen(client, service) -> None:  # noqa: ANN001
    body = client.post("/rag/search", json={"query": "pool", "retriever": "bm25"}).json()

    assert body["retriever"] == "bm25"


def test_an_unknown_retriever_is_rejected(client, service) -> None:  # noqa: ANN001
    response = client.post("/rag/search", json={"query": "pool", "retriever": "magic"})

    assert response.status_code == 422


def test_an_empty_result_is_a_200_rather_than_a_404(client) -> None:  # noqa: ANN001
    # "The knowledge base has nothing about this" is an answer the agent reasons about, and
    # Phase 7 treats the absence of a precedent as evidence.
    stub = _StubService(hits=[])
    app.dependency_overrides[rag_api.get_service] = lambda: stub

    response = client.post("/rag/search", json={"query": "kubernetes ingress"})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_a_search_is_recorded_in_the_retrieval_log(client, service) -> None:  # noqa: ANN001
    client.post(
        "/rag/search",
        json={
            "query": "pool",
            "filters": {"service": "orders"},
            "investigation_id": "0f9b7a1e-6a0e-4d2f-9f4a-2c8e1b3d5a70",
        },
    )

    logged = service.store.logged

    assert len(logged) == 1
    assert logged[0]["filters"] == {"service": "orders"}
    assert logged[0]["investigation_id"] == "0f9b7a1e-6a0e-4d2f-9f4a-2c8e1b3d5a70"


def test_an_invalid_filter_is_refused_rather_than_dropped(client, service) -> None:  # noqa: ANN001
    # A silently dropped filter returns more than was asked for, and "why is a payments runbook
    # in my orders search" is a much harder question than a 422.
    response = client.post("/rag/search", json={"query": "pool", "filters": {"service": []}})

    assert response.status_code == 422
    assert "no values" in response.json()["detail"]


def test_a_dead_embedding_runtime_is_a_503_naming_the_fix(client) -> None:  # noqa: ANN001
    stub = _StubService()
    stub.retrievers["hybrid"] = _StubRetriever(
        "hybrid",
        [],
        EmbeddingUnavailableError(
            "Ollama does not have model 'bge-m3'. Run: ollama pull bge-m3"
        ),
    )
    app.dependency_overrides[rag_api.get_service] = lambda: stub

    response = client.post("/rag/search", json={"query": "pool"})

    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert "ollama pull" in response.json()["detail"]


def test_an_empty_query_is_rejected(client, service) -> None:  # noqa: ANN001
    assert client.post("/rag/search", json={"query": ""}).status_code == 422


def test_the_index_is_loaded_before_the_first_search(client, service) -> None:  # noqa: ANN001
    assert not service.index_ready

    client.post("/rag/search", json={"query": "pool"})

    assert service.index_ready


# -------------------------------------------------------------------- ingest ----


def test_ingest_summarises_what_it_did(client, service) -> None:  # noqa: ANN001
    body = client.post("/rag/ingest", json={}).json()

    assert body["ingested"] == 1
    assert body["unchanged"] == 1
    assert body["removed"] == 1
    assert body["failed"] == 1
    assert body["chunks_written"] == 3
    assert body["duration_ms"] == 812


def test_a_failed_document_is_named_in_the_response(client, service) -> None:  # noqa: ANN001
    # A document silently missing from the knowledge base looks exactly like a knowledge base
    # that has nothing to say.
    documents = client.post("/rag/ingest", json={}).json()["documents"]
    failed = [d for d in documents if d["status"] == "failed"]

    assert failed and failed[0]["error"] == "no front matter"


def test_ingest_defaults_to_pruning(client, service) -> None:  # noqa: ANN001
    client.post("/rag/ingest", json={})

    _, force, prune = service.pipeline.calls[0]

    assert force is False
    assert prune is True


def test_ingest_passes_force_through(client, service) -> None:  # noqa: ANN001
    client.post("/rag/ingest", json={"force": True, "prune": False})

    _, force, prune = service.pipeline.calls[0]

    assert force is True
    assert prune is False


def test_ingest_without_an_embedding_runtime_is_a_503(client, service) -> None:  # noqa: ANN001
    service.pipeline.error = EmbeddingUnavailableError("Could not reach Ollama.")

    assert client.post("/rag/ingest", json={}).status_code == 503


# --------------------------------------------------------------------- stats ----


def test_stats_reports_the_corpus_and_whether_retrieval_can_run(client, service) -> None:  # noqa: ANN001
    body = client.get("/rag/stats").json()

    assert body["documents"] == 28
    assert body["chunks"] == 94
    assert body["documents_by_type"]["postmortem"] == 10
    assert body["lexical_index_size"] == 94
    # A populated index with no reachable embedding model answers lexical searches and not dense
    # ones, and that is worth being able to see before a search returns half of what it should.
    assert body["embedding_model"] == "bge-m3"
    assert body["embedding_available"] is True


def test_stats_says_why_the_reranker_is_unavailable(client, service) -> None:  # noqa: ANN001
    # The cross-encoder is optional, so "not installed" is an ordinary state rather than a
    # fault — but a hybrid_rerank search silently returning fused results is not something
    # anyone should have to infer from the ranking.
    body = client.get("/rag/stats").json()

    assert body["rerank_model"] == "BAAI/bge-reranker-v2-m3"
    assert body["rerank_available"] is False
    assert "sentence-transformers" in body["rerank_unavailable_reason"]


# ------------------------------------------------------------------- context ----


def test_a_search_can_return_a_prompt_ready_context(client, service) -> None:  # noqa: ANN001
    body = client.post("/rag/search", json={"query": "pool", "build_context": True}).json()

    context = body["context"]

    # The label is what lets the agent cite a source and the UI resolve the citation back to a
    # chunk id; without it the retrieved text is an assertion rather than evidence.
    assert "[S1] runbook · orders · RB-001 · Runbook > Fix" in context["text"]
    assert context["sources"][0]["chunk_id"] == "chunk-1"
    assert 0 < context["token_count"] <= 3000


def test_context_is_off_unless_it_is_asked_for(client, service) -> None:  # noqa: ANN001
    # A caller that renders the hits itself would otherwise pay for a second copy of them.
    assert client.post("/rag/search", json={"query": "pool"}).json()["context"] is None
