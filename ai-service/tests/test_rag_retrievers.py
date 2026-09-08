"""Fusion, and what the hybrid retriever does when half of it is unavailable.

Fusion is tested on rankings rather than through a store, because that is what Reciprocal Rank
Fusion actually operates on: given two orderings, the combined ordering is a property of the
positions alone, and a test that goes through pgvector to establish it is testing pgvector.
"""

from __future__ import annotations

import pytest

from rag.documents import RetrievedChunk, SourceType, StoredChunk
from rag.embeddings import EmbeddingError, EmbeddingProvider
from rag.lexical import Bm25Index
from rag.retrievers import (
    Bm25Retriever,
    HybridRetriever,
    VectorRetriever,
    fuse,
)
from rag.store import StoreStats, VectorStore


def _stored(chunk_id: str, content: str = "content") -> StoredChunk:
    return StoredChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        content=content,
        chunk_index=0,
        title=chunk_id,
        source_type=SourceType.RUNBOOK,
    )


def _ranking(*chunk_ids: str) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(chunk=_stored(c), score=1.0 / rank, rank=rank, retriever="test")
        for rank, c in enumerate(chunk_ids, start=1)
    ]


# ------------------------------------------------------------------------ fusion ----


def test_a_chunk_both_lists_rank_highly_beats_one_that_either_ranks_first() -> None:
    """The property that makes fusion worth doing.

    `b` is second in both lists and `a` is first in one and absent from the other. With k = 60
    the top positions are nearly equal in value, so two agreeing votes outweigh one strong one —
    which is exactly the behaviour that lets a lexical match and a semantic match reinforce each
    other instead of competing.
    """
    fused = fuse([_ranking("a", "b", "c"), _ranking("d", "b", "e")], k=3)

    assert fused[0].chunk_id == "b"


def test_fusion_deduplicates() -> None:
    fused = fuse([_ranking("a", "b"), _ranking("b", "a")], k=10)

    assert [f.chunk_id for f in fused] == ["a", "b"]
    assert len(fused) == 2


def test_fused_ranks_are_renumbered_from_one() -> None:
    fused = fuse([_ranking("a", "b", "c"), _ranking("c", "b", "a")], k=3)

    assert [f.rank for f in fused] == [1, 2, 3]
    assert all(f.retriever == "hybrid" for f in fused)


def test_ties_break_deterministically() -> None:
    # Same query, same corpus, same order — otherwise a retrieval evaluation measures the sort
    # rather than the retriever.
    first = fuse([_ranking("a", "b"), _ranking("b", "a")], k=2)
    second = fuse([_ranking("a", "b"), _ranking("b", "a")], k=2)

    assert [f.chunk_id for f in first] == [f.chunk_id for f in second]


def test_fusion_of_one_list_preserves_its_order() -> None:
    fused = fuse([_ranking("a", "b", "c")], k=3)

    assert [f.chunk_id for f in fused] == ["a", "b", "c"]


def test_fusion_of_nothing_is_nothing() -> None:
    assert fuse([[], []], k=5) == []


# ------------------------------------------------------------------- the hybrid ----


class _FakeEmbeddings(EmbeddingProvider):
    def __init__(self, working: bool = True) -> None:
        self._working = working

    @property
    def model(self) -> str:
        return "fake"

    @property
    def dimensions(self) -> int:
        return 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._working:
            raise EmbeddingError("no embedding runtime")

        return [[1.0, 0.0, 0.0] for _ in texts]

    async def is_available(self) -> bool:
        return self._working


class _FakeStore(VectorStore):
    def __init__(self, chunks: list[StoredChunk], working: bool = True) -> None:
        self._chunks = chunks
        self._working = working

    async def upsert(self, document, chunks, embeddings):  # noqa: ANN001, ANN201
        raise NotImplementedError

    async def search(self, embedding, k, filters=None):  # noqa: ANN001, ANN201
        if not self._working:
            raise RuntimeError("no database")

        return [
            RetrievedChunk(chunk=c, score=0.9, rank=rank, retriever="vector")
            for rank, c in enumerate(self._chunks[:k], start=1)
        ]

    async def all_chunks(self) -> list[StoredChunk]:
        return list(self._chunks)

    async def content_hashes(self) -> dict[str, str]:
        return {}

    async def delete_missing(self, keys: set[str]) -> int:
        return 0

    async def stats(self) -> StoreStats:
        return StoreStats(len(self._chunks), len(self._chunks), {}, {})


POOL = _stored("pool", "connection pool exhausted, raise MaxPoolSize back to 200")
LEAK = _stored("leak", "working set climbs monotonically, a memory leak")


def _hybrid(embeddings_working: bool = True, store_working: bool = True) -> HybridRetriever:
    index = Bm25Index()
    index.build([POOL, LEAK])

    return HybridRetriever(
        Bm25Retriever(index),
        VectorRetriever(_FakeEmbeddings(embeddings_working), _FakeStore([LEAK], store_working)),
        candidates=10,
    )


async def test_the_hybrid_reports_both_candidate_lists() -> None:
    result = await _hybrid().retrieve("connection pool", k=5)

    assert result.candidates["bm25"] == ["pool"]
    assert result.candidates["vector"] == ["leak"]
    assert result.candidates["fused"] == [c.chunk_id for c in result.chunks]


async def test_the_hybrid_survives_a_dead_embedding_runtime() -> None:
    # Half a search is worth more than none: the lexical half needs neither Ollama nor a
    # database, and an incident is a bad moment to lose retrieval entirely.
    result = await _hybrid(embeddings_working=False).retrieve("connection pool", k=5)

    assert [c.chunk_id for c in result.chunks] == ["pool"]
    assert result.candidates["vector"] == []


async def test_the_hybrid_survives_a_dead_database() -> None:
    result = await _hybrid(store_working=False).retrieve("connection pool", k=5)

    assert [c.chunk_id for c in result.chunks] == ["pool"]


class _BrokenIndex(Bm25Index):
    def search(self, query, k, filters=None):  # noqa: ANN001, ANN201, ARG002
        raise RuntimeError("the lexical index is not usable")


async def test_both_halves_failing_raises_rather_than_returning_nothing() -> None:
    """An empty result is a finding; two dead retrievers are not.

    "The knowledge base has nothing about this" and "retrieval is broken" have to look different
    to the agent, or a broken index reads as an incident with no precedent. Note that an *empty*
    lexical index is the first case, not the second: it answered, and the answer was nothing.
    """
    retriever = HybridRetriever(
        Bm25Retriever(_BrokenIndex()),
        VectorRetriever(_FakeEmbeddings(False), _FakeStore([], False)),
    )

    with pytest.raises(RuntimeError, match="Neither the lexical nor the dense"):
        await retriever.retrieve("anything", k=5)


async def test_an_empty_corpus_returns_no_hits_without_failing() -> None:
    retriever = HybridRetriever(
        Bm25Retriever(Bm25Index()),
        VectorRetriever(_FakeEmbeddings(), _FakeStore([])),
    )

    result = await retriever.retrieve("anything", k=5)

    assert result.chunks == []


async def test_stage_latencies_are_reported_per_half() -> None:
    result = await _hybrid().retrieve("connection pool", k=5)

    assert "bm25_total" in result.stage_latency_ms
    assert "vector_embed" in result.stage_latency_ms
    # Wall clock, not the sum of the halves: they run concurrently, and adding them up would
    # report a wait nobody did.
    assert result.total_latency_ms >= 0
