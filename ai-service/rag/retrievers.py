"""Four ways to find chunks, behind one interface.

The interface is the point. ``evaluation/rag_eval.py`` measures recall@5 for BM25, dense, hybrid
and hybrid-plus-reranker over the same query set, and a comparison is only worth reading if the
things compared are interchangeable — same call, same filters, same result shape. All four live
here; the fourth is the third with a cross-encoder on the end and needs nothing else from it.

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
from rag.expansion import QueryExpander
from rag.lexical import LexicalIndex
from rag.rerank import Reranker, RerankUnavailableError
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

# At most this many chunks of any one document in a final result.
#
# Measured, not guessed, and worth less than the first measurement suggested. Adjacent chunks of
# one runbook score alike, so without a cap one document takes three of five slots and the second
# document the question needed sits at rank six. Over the 120-query benchmark the cap is worth
# about a point of recall@5 (hybrid_rerank 0.973 against 0.964, hybrid 0.944 against 0.939) and
# costs about four of precision@5, and it rescues no query that was otherwise lost — the miss
# lists are identical with it and without it. What it recovers is the *second* relevant document
# of a query that has two, which is 56 of the 120 and is what an investigation wanting both the
# runbook and the postmortem needs. `--no-diversity` reproduces the comparison.
#
# Two rather than one: a runbook's symptom and its fix are different chunks and an investigation
# usually wants both, so forcing one chunk per document would trade a document the answer needs
# for half of a document it already had.
DEFAULT_MAX_PER_DOCUMENT = 2

# When a cap is in force, the underlying search is asked for this multiple of k, because a
# capped selection can only skip what it was given. Four covers the worst realistic case — the
# top eight chunks all belonging to two documents — without making a small search large.
CAP_OVER_FETCH = 4


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

    def __init__(self, index: LexicalIndex, max_per_document: int | None = None) -> None:
        self._index = index
        self._max_per_document = max_per_document

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
        fetched = self._index.search(query, _fetch_for(k, self._max_per_document), filters)
        chunks = cap_per_document(fetched, k, self._max_per_document)
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
    """Dense only. Finds the runbook that describes the symptom in different words.

    With an ``expander`` it searches twice: once for the question, once for the passage the
    local model thinks would answer it, fusing the two rankings. See :mod:`rag.expansion` for
    what that buys and why the hypothesis is fused rather than substituted.
    """

    def __init__(
        self,
        embeddings: EmbeddingProvider,
        store: VectorStore,
        max_per_document: int | None = None,
        expander: QueryExpander | None = None,
        rrf_k: int = RRF_K,
    ) -> None:
        self._embeddings = embeddings
        self._store = store
        self._max_per_document = max_per_document
        self._expander = expander
        self._rrf_k = rrf_k

    @property
    def name(self) -> str:
        return f"vector_{self._expander.name}" if self._expander else "vector"

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        wanted = _fetch_for(k, self._max_per_document)
        stages: dict[str, int] = {}

        expansion = None

        if self._expander is not None:
            expansion = await self._expander.expand(query)
            stages["expand"] = expansion.latency_ms if expansion else _ms_since(started)

        embed_started = time.perf_counter()
        vector = await self._embeddings.embed_query(query)
        stages["embed"] = _ms_since(embed_started)

        search_started = time.perf_counter()
        fetched = await self._store.search(vector, wanted, filters)
        stages["vector"] = _ms_since(search_started)

        candidates = {"vector": [c.chunk_id for c in fetched]}
        ranked = fetched

        if expansion is not None:
            # The hypothesis is embedded as a document rather than as a query. It is written to
            # look like a corpus passage, which is the whole trick, and bge-m3 is symmetric so
            # today the two paths are the same call — but the seam is where an asymmetric model
            # would need them to differ, and putting the hypothesis on the wrong side of it is a
            # silent quality loss rather than an error.
            hyde_started = time.perf_counter()
            hyde_vectors = await self._embeddings.embed([expansion.text])
            hyde_chunks = await self._store.search(hyde_vectors[0], wanted, filters)
            stages["hyde"] = _ms_since(hyde_started)

            candidates["hyde"] = [c.chunk_id for c in hyde_chunks]
            ranked = fuse([fetched, hyde_chunks], wanted, self._rrf_k, retriever=self.name)

        chunks = cap_per_document(ranked, k, self._max_per_document)
        stages["total"] = _ms_since(started)

        return RetrievalResult(
            query=query,
            retriever=self.name,
            chunks=chunks,
            stage_latency_ms=stages,
            candidates={**candidates, "final": [c.chunk_id for c in chunks]},
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
        max_per_document: int | None = None,
    ) -> None:
        self._lexical = lexical
        self._vector = vector
        self._candidates = candidates
        self._rrf_k = rrf_k
        self._max_per_document = max_per_document

    @property
    def name(self) -> str:
        # An expanded dense half makes this a different pipeline, and a benchmark row named
        # "hybrid" for two different pipelines is a table nobody can act on.
        return "hybrid_hyde" if self._vector.name != "vector" else "hybrid"

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

        ranked = fuse(
            [lexical.chunks, vector.chunks],
            _fetch_for(k, self._max_per_document),
            self._rrf_k,
        )
        fused = cap_per_document(ranked, k, self._max_per_document)

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


class HybridRerankRetriever(Retriever):
    """Hybrid retrieval with a cross-encoder as the third vote.

    The hybrid search is asked for ``candidates`` chunks rather than ``k``, because that is the
    whole point: fusion is being used as a fast filter that gets the right chunk into the top
    thirty, and the reranker is being used to get it into the top five. Asking the fused search
    for five and reranking those five reorders a list that was already the answer.

    ``blend`` decides what the reranker's opinion is worth, and it is ``True`` because of a
    measurement rather than a preference. Letting the cross-encoder replace the fused order
    outright — the benchmark's ``rerank_only`` row — is better at the top of the ranking and
    worse below it: over 120 queries it wins recall@1 (0.655 against 0.642), success@1 (0.867
    against 0.850), MRR and precision, and loses three queries the blend keeps against one. Its
    cross-lingual recall is 0.808, below both the blend's 0.904 and plain fusion's 0.846, so on
    the one property bge-m3 was chosen for the cross-encoder is the weaker of the two models.

    Depth is the column that decides what a prompt contains: five chunks go into the context, and
    whether the right document is first or third among them costs nothing where its absence costs
    the answer. So the cross-encoder is a second ranking to fuse with rather than a verdict that
    overrides one, combined the same way BM25 and the dense search are: reciprocal rank.

    Set ``blend=False`` for the replace-everything behaviour. Keeping it runnable is what makes
    the paragraph above checkable rather than a claim.

    Blended, this is the best row in the table — R@3 0.956 against plain fusion's 0.910, R@5
    0.973 against 0.944, and `symptom` recall up from 0.950 to 0.983. What it is not is the
    default: it is two to three times the latency of a fused search with a GPU, and 21.5 s per
    search on a CPU-only PyTorch install (docs/adr/0005-reranker-runs-in-process.md).
    """

    def __init__(
        self,
        hybrid: HybridRetriever,
        reranker: Reranker,
        candidates: int = DEFAULT_CANDIDATES,
        degrade: bool = True,
        blend: bool = True,
        rrf_k: int = RRF_K,
        max_per_document: int | None = None,
    ) -> None:
        self._hybrid = hybrid
        self._reranker = reranker
        self._candidates = candidates
        self._degrade = degrade
        self._blend = blend
        self._rrf_k = rrf_k
        self._max_per_document = max_per_document

    @property
    def name(self) -> str:
        # Follows the pipeline underneath, so an expanded hybrid with a reranker on it is not
        # reported under the same name as a plain one.
        if not self._blend:
            return "rerank_only"

        return "hybrid_rerank" if self._hybrid.name == "hybrid" else f"{self._hybrid.name}_rerank"

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()
        fused = await self._hybrid.retrieve(query, self._candidates, filters)

        rerank_started = time.perf_counter()

        # Every candidate is scored, not just the ones that would survive. Blending needs the
        # reranker's opinion of the whole list — a chunk it puts twelfth is information — and in
        # replace mode the tail is truncated a line later anyway, at no extra cost: the model
        # has already read every pair either way.
        wanted = len(fused.chunks) if self._blend else k

        try:
            reranked = await self._reranker.rerank(query, fused.chunks, wanted)
        except RerankUnavailableError as exc:
            if not self._degrade:
                raise

            # The reranker is an optional dependency and the model is a 2.2 GB download, so a
            # deployment without it is expected rather than broken. Degrading to the fused order
            # returns the same chunks in a worse order, which is the right trade for a search
            # request — but the result says so, in the stage map and by having no `reranked`
            # candidate list, because an evaluation row that silently reports fused numbers
            # under the reranker's name is worse than a missing row.
            logger.warning("Reranking skipped: %s", exc)

            return RetrievalResult(
                query=query,
                retriever=self.name,
                chunks=cap_per_document(fused.chunks, k, self._max_per_document),
                stage_latency_ms={**fused.stage_latency_ms, "total": _ms_since(started)},
                candidates={**fused.candidates, "rerank_skipped": []},
                filters=fused.filters,
            )

        blended = (
            fuse(
                [fused.chunks, reranked],
                _fetch_for(k, self._max_per_document),
                self._rrf_k,
                retriever=self.name,
            )
            if self._blend
            else reranked
        )
        chunks = cap_per_document(blended, k, self._max_per_document)

        return RetrievalResult(
            query=query,
            retriever=self.name,
            chunks=chunks,
            stage_latency_ms={
                **{k_: v for k_, v in fused.stage_latency_ms.items() if k_ != "total"},
                "fusion": fused.total_latency_ms,
                "rerank": _ms_since(rerank_started),
                "total": _ms_since(started),
            },
            candidates={
                **fused.candidates,
                # The reranker's own order, before blending. Which of the two rankings put a
                # chunk in the answer is the question a surprising result raises, and after
                # fusion it is not recoverable from the result alone.
                "reranked": [c.chunk_id for c in reranked],
                "final": [c.chunk_id for c in chunks],
            },
            filters=fused.filters,
        )


def cap_per_document(
    chunks: list[RetrievedChunk],
    k: int,
    max_per_document: int | None,
) -> list[RetrievedChunk]:
    """The best ``k`` chunks, with no document allowed more than ``max_per_document`` of them.

    Order is preserved; a chunk is skipped rather than moved, so the result is what the ranking
    already said minus the repetition. Ranks are renumbered, because the position a consumer
    reads has to be the position in what it was given.

    ``None`` disables the cap, which is what a candidate pool wants: the reranker should see a
    document's third chunk if the ranking put it there, and the cap belongs at the end of the
    pipeline rather than in the middle of it.
    """
    if not max_per_document:
        return chunks[:k]

    kept: list[RetrievedChunk] = []
    seen: dict[str, int] = {}

    for chunk in chunks:
        document = chunk.chunk.document_id
        taken = seen.get(document, 0)

        if taken >= max_per_document:
            continue

        seen[document] = taken + 1
        kept.append(chunk)

        if len(kept) == k:
            break

    return [
        chunk.reranked(rank=rank, score=chunk.score, retriever=chunk.retriever)
        for rank, chunk in enumerate(kept, start=1)
    ]


def fuse(
    rankings: list[list[RetrievedChunk]],
    k: int,
    rrf_k: int = RRF_K,
    retriever: str = "hybrid",
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion over any number of ranked lists.

    Used twice: over BM25 and the dense search, and again over that result and the reranker's
    ordering of it. The second use is the same operation on the same grounds — two rankings
    whose scores are not comparable, whose positions are.

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
        chunks[chunk_id].reranked(rank=rank, score=score, retriever=retriever)
        for rank, (chunk_id, score) in enumerate(ordered, start=1)
    ]


def _fetch_for(k: int, max_per_document: int | None) -> int:
    """How many chunks to ask for so that a capped selection can still return ``k``.

    Without a cap this is ``k``. With one it is a multiple, because the cap can only skip what
    it was handed: asking for five and then refusing three of them returns two.
    """
    return k if not max_per_document else k * CAP_OVER_FETCH


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
