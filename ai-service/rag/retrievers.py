"""Four ways to find chunks, behind one interface.

The interface is the point. Phase 6 measures recall@5 for BM25, dense, hybrid, and hybrid with a
reranker over the same query set, and a comparison is only worth reading if the things compared
are interchangeable — same call, same filters, same result shape. Three of the four live here;
``HybridRerankRetriever`` wraps this one in Phase 6 and needs nothing new from it.

Fusion is Reciprocal Rank Fusion. It combines rankings rather than scores, which matters because
BM25 term weights and cosine similarities are not on the same scale and no amount of
normalisation makes them so — a cosine of 0.82 and a BM25 score of 11.4 are not comparable
quantities, but "first" and "third" are.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from rag.documents import RetrievedChunk
from rag.embeddings import EmbeddingProvider
from rag.lexical import LexicalIndex
from rag.store import VectorStore

logger = logging.getLogger(__name__)

# The constant in 1 / (k + rank). Cormack et al. found 60 works across collections without
# tuning, and it is what docs/planning.md fixed. Its effect is to flatten the top of each list:
# with k = 60, first place is worth 1/61 and third 1/63, so a chunk both retrievers rank highly
# beats one that either ranks first alone.
RRF_K = 60

# How many candidates each retriever contributes to a fusion. Larger than the returned k because
# fusion can only promote what it was given, and the whole value of hybrid retrieval is the
# chunk that one side ranked twelfth and the other ranked second.
DEFAULT_CANDIDATES = 30


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """What a retriever found, and what it cost.

    ``stage_latency_ms`` and ``candidates`` exist for ``rag.retrieval_logs``: when a hybrid
    search returns something surprising, the answerable question is which half proposed it, and
    that is only answerable if both halves' candidate lists were kept.
    """

    query: str
    retriever: str
    chunks: list[RetrievedChunk]
    stage_latency_ms: dict[str, int] = field(default_factory=dict)
    candidates: dict[str, list[str]] = field(default_factory=dict)
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def total_latency_ms(self) -> int:
        """Wall clock, not the sum of the stages — the two halves of a hybrid search run
        concurrently, so adding them up would report a wait that nobody did."""
        return self.stage_latency_ms.get("total", 0)


class Retriever(abc.ABC):
    """Something that turns a query into ranked chunks."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Identifier used in evaluation tables and in ``retrieval_logs``."""

    @abc.abstractmethod
    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        """The ``k`` best chunks for ``query``, best first."""


class Bm25Retriever(Retriever):
    """Lexical only. The baseline the others have to beat, and the one that finds error codes."""

    def __init__(self, index: LexicalIndex) -> None:
        self._index = index

    @property
    def name(self) -> str:
        return "bm25"

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        chunks = self._index.search(query, k, filters)
        elapsed = _ms_since(started)

        return RetrievalResult(
            query=query,
            retriever=self.name,
            chunks=chunks,
            stage_latency_ms={"bm25": elapsed, "total": elapsed},
            candidates={"bm25": [c.chunk_id for c in chunks]},
            filters=filters or {},
        )


class VectorRetriever(Retriever):
    """Dense only. Finds the runbook that describes the symptom in different words."""

    def __init__(self, embeddings: EmbeddingProvider, store: VectorStore) -> None:
        self._embeddings = embeddings
        self._store = store

    @property
    def name(self) -> str:
        return "vector"

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        vector = await self._embeddings.embed_query(query)
        embed_ms = _ms_since(started)

        search_started = time.perf_counter()
        chunks = await self._store.search(vector, k, filters)
        search_ms = _ms_since(search_started)

        return RetrievalResult(
            query=query,
            retriever=self.name,
            chunks=chunks,
            stage_latency_ms={
                "embed": embed_ms,
                "vector": search_ms,
                "total": _ms_since(started),
            },
            candidates={"vector": [c.chunk_id for c in chunks]},
            filters=filters or {},
        )


class HybridRetriever(Retriever):
    """BM25 and dense, run concurrently and fused by reciprocal rank."""

    def __init__(
        self,
        lexical: Bm25Retriever,
        vector: VectorRetriever,
        candidates: int = DEFAULT_CANDIDATES,
        rrf_k: int = RRF_K,
    ) -> None:
        self._lexical = lexical
        self._vector = vector
        self._candidates = candidates
        self._rrf_k = rrf_k

    @property
    def name(self) -> str:
        return "hybrid"

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()

        lexical, vector = await asyncio.gather(
            self._lexical.retrieve(query, self._candidates, filters),
            self._vector.retrieve(query, self._candidates, filters),
            # A dense search needs Ollama and a database; a lexical one needs neither. Letting
            # one failure take down a search that the other half could still answer would make
            # the hybrid retriever less available than either of its parts.
            return_exceptions=True,
        )

        lexical = _or_empty(lexical, "bm25", query)
        vector = _or_empty(vector, "vector", query)

        if not lexical.chunks and not vector.chunks:
            _reraise_if_both_failed(lexical, vector)

        fused = fuse([lexical.chunks, vector.chunks], k, self._rrf_k)

        return RetrievalResult(
            query=query,
            retriever=self.name,
            chunks=fused,
            stage_latency_ms={
                **{f"bm25_{key}": value for key, value in lexical.stage_latency_ms.items()},
                **{f"vector_{key}": value for key, value in vector.stage_latency_ms.items()},
                "total": _ms_since(started),
            },
            candidates={
                "bm25": [c.chunk_id for c in lexical.chunks],
                "vector": [c.chunk_id for c in vector.chunks],
                "fused": [c.chunk_id for c in fused],
            },
            filters=filters or {},
        )


def fuse(
    rankings: list[list[RetrievedChunk]],
    k: int,
    rrf_k: int = RRF_K,
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion over any number of ranked lists.

    Ties break on the chunk's best position in any single list, then on its id. Without a
    deterministic tiebreak the same query returns the same chunks in a different order on
    different runs, which turns a retrieval evaluation into a coin flip.
    """
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    chunks: dict[str, RetrievedChunk] = {}

    for ranking in rankings:
        for chunk in ranking:
            chunk_id = chunk.chunk_id
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (rrf_k + chunk.rank)
            best_rank[chunk_id] = min(best_rank.get(chunk_id, chunk.rank), chunk.rank)
            chunks.setdefault(chunk_id, chunk)

    ordered = sorted(
        scores.items(),
        key=lambda item: (-item[1], best_rank[item[0]], item[0]),
    )[:k]

    return [
        chunks[chunk_id].reranked(rank=rank, score=score, retriever="hybrid")
        for rank, (chunk_id, score) in enumerate(ordered, start=1)
    ]


def _or_empty(
    result: RetrievalResult | BaseException,
    retriever: str,
    query: str,
) -> RetrievalResult:
    if isinstance(result, RetrievalResult):
        return result

    logger.warning("The %s half of the hybrid search failed: %s", retriever, result)

    return RetrievalResult(
        query=query,
        retriever=retriever,
        chunks=[],
        stage_latency_ms={"error": 0},
        candidates={retriever: []},
    )


def _reraise_if_both_failed(*results: RetrievalResult) -> None:
    """An empty result is a finding; two dead retrievers are not.

    "The knowledge base has nothing about this" and "retrieval is broken" have to look different
    to the agent, or a broken index reads as an incident with no precedent.
    """
    if all("error" in r.stage_latency_ms for r in results):
        raise RuntimeError(
            "Neither the lexical nor the dense retriever answered. "
            "Check that Ollama is running and the rag schema has been ingested."
        )


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
