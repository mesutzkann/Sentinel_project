"""Fusion, and what the hybrid retriever does when half of it is unavailable.

Fusion is tested on rankings rather than through a store, because that is what Reciprocal Rank
Fusion actually operates on: given two orderings, the combined ordering is a property of the
positions alone, and a test that goes through pgvector to establish it is testing pgvector.
"""

from __future__ import annotations

import pytest

from rag.documents import RetrievedChunk, SourceType, StoredChunk
from rag.embeddings import EmbeddingError, EmbeddingProvider
from rag.expansion import Expansion, QueryExpander
from rag.lexical import Bm25Index
from rag.rerank import Reranker, RerankUnavailableError
from rag.retrievers import (
    Bm25Retriever,
    HybridRerankRetriever,
    HybridRetriever,
    VectorRetriever,
    cap_per_document,
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


# ------------------------------------------------------------ the hybrid, reranked ----


class _FakeReranker(Reranker):
    """Puts the candidate whose content mentions the query last, first."""

    def __init__(self, unavailable: bool = False) -> None:
        self._unavailable = unavailable
        self.seen: list[str] = []

    @property
    def name(self) -> str:
        return "fake_reranker"

    @property
    def model(self) -> str:
        return "fake"

    async def rerank(self, query, chunks, k):  # noqa: ANN001, ANN201
        if self._unavailable:
            raise RerankUnavailableError("sentence-transformers is not installed")

        self.seen = [c.chunk_id for c in chunks]

        return [
            c.reranked(rank=rank, score=1.0 / rank, retriever=self.name)
            for rank, c in enumerate(reversed(chunks), start=1)
        ][:k]

    async def is_available(self) -> bool:
        return not self._unavailable


class _FakeSpite(Reranker):
    """Ranks one chunk last and everything else in the order it arrived."""

    def __init__(self, hated: str) -> None:
        self._hated = hated

    @property
    def name(self) -> str:
        return "fake_spite"

    @property
    def model(self) -> str:
        return "fake"

    async def rerank(self, query, chunks, k):  # noqa: ANN001, ANN201
        ordered = [c for c in chunks if c.chunk_id != self._hated]
        ordered += [c for c in chunks if c.chunk_id == self._hated]

        return [
            c.reranked(rank=rank, score=1.0 / rank, retriever=self.name)
            for rank, c in enumerate(ordered, start=1)
        ][:k]

    async def is_available(self) -> bool:
        return True


async def test_the_reranker_gets_the_candidate_pool_not_the_final_five() -> None:
    """The reason the two stages compose.

    Fusion is a fast filter that has to get the right chunk into the top thirty; the reranker
    turns thirty into five. Handing it five would rerank a list that was already the answer.
    """
    reranker = _FakeReranker()
    retriever = HybridRerankRetriever(_hybrid(), reranker, candidates=10)

    await retriever.retrieve("connection pool", k=1)

    assert len(reranker.seen) == 2


async def test_the_reranker_decides_the_final_order_when_it_replaces() -> None:
    retriever = HybridRerankRetriever(_hybrid(), _FakeReranker(), candidates=10, blend=False)

    result = await retriever.retrieve("connection pool", k=2)

    # Fusion ranks the two candidates leak, pool; the fake reranker reverses whatever it is
    # given, so an order of pool, leak is the cross-encoder having had the last word.
    assert result.retriever == "rerank_only"
    assert [c.chunk_id for c in result.chunks] == ["pool", "leak"]
    assert result.candidates["reranked"] == ["pool", "leak"]


async def test_blending_keeps_what_fusion_was_sure_about() -> None:
    """Why the shipped retriever blends rather than replaces.

    Measured: letting the cross-encoder overrule fusion outright cost recall@5 on Turkish
    queries against the English corpus — 0.846 to 0.808 — because it demoted the postmortem
    fusion had ranked first. Here the reranker hates `pool`, which fusion and BM25 both put at
    the top, and blending keeps it in the answer instead of dropping it.
    """
    reranker = _FakeSpite(hated="pool")
    retriever = HybridRerankRetriever(_hybrid(), reranker, candidates=10)

    result = await retriever.retrieve("connection pool", k=2)

    assert result.retriever == "hybrid_rerank"
    assert "pool" in [c.chunk_id for c in result.chunks]


async def test_blending_still_lets_the_reranker_move_things() -> None:
    """The other half of the trade, at the level it actually happens.

    Fusion ranks a, b, c; the reranker promotes c from last to first and pushes b down. c
    overtakes b. What it cannot do is discard a, which both stages rank highly — that is the
    conservatism the measurement bought.
    """
    blended = fuse(
        [_ranking("a", "b", "c"), _ranking("c", "a", "b")], k=3, retriever="hybrid_rerank"
    )

    assert [c.chunk_id for c in blended] == ["a", "c", "b"]
    assert all(c.retriever == "hybrid_rerank" for c in blended)


async def test_both_orderings_are_kept_on_the_result() -> None:
    # Which of the two rankings put a chunk in the answer is the question a surprising result
    # raises, and after fusion it is not recoverable from the result alone.
    result = await HybridRerankRetriever(_hybrid(), _FakeReranker(), candidates=10).retrieve(
        "connection pool", k=2
    )

    assert result.candidates["reranked"] == ["pool", "leak"]
    assert result.candidates["fused"] == ["leak", "pool"]
    assert result.candidates["final"] == [c.chunk_id for c in result.chunks]


async def test_the_fused_candidates_are_kept_alongside_the_reranked_ones() -> None:
    # Which stage promoted a chunk is the only thing that explains a surprising result, and it
    # cannot be reconstructed after the fact.
    result = await HybridRerankRetriever(_hybrid(), _FakeReranker(), candidates=10).retrieve(
        "connection pool", k=2
    )

    assert result.candidates["bm25"] == ["pool"]
    assert result.candidates["fused"]
    assert "rerank" in result.stage_latency_ms


async def test_a_search_degrades_to_the_fused_order_without_the_cross_encoder() -> None:
    """The optional dependency is missing on a fresh checkout, which is not an outage.

    The chunks are the same ones in a worse order, and the result says so by carrying no
    `reranked` list — a caller cannot mistake this for a reranked search.
    """
    retriever = HybridRerankRetriever(_hybrid(), _FakeReranker(unavailable=True), candidates=10)

    result = await retriever.retrieve("connection pool", k=2)

    assert [c.chunk_id for c in result.chunks] == ["leak", "pool"]  # the fused order
    assert "reranked" not in result.candidates
    assert "rerank_skipped" in result.candidates


async def test_the_benchmark_refuses_to_degrade() -> None:
    # A table whose hybrid_rerank row is quietly the fused numbers is worse than a missing row.
    retriever = HybridRerankRetriever(
        _hybrid(), _FakeReranker(unavailable=True), candidates=10, degrade=False
    )

    with pytest.raises(RerankUnavailableError):
        await retriever.retrieve("connection pool", k=2)


# --------------------------------------------------------- one document, one voice ----


def _from_document(chunk_id: str, document_id: str, rank: int) -> RetrievedChunk:
    stored = StoredChunk(
        chunk_id=chunk_id,
        document_id=document_id,
        content="content",
        chunk_index=0,
        title=document_id,
        source_type=SourceType.RUNBOOK,
    )

    return RetrievedChunk(chunk=stored, score=1.0 / rank, rank=rank, retriever="hybrid")


def test_a_document_cannot_take_every_slot() -> None:
    """The measured failure this cap exists for.

    Adjacent chunks of one runbook score alike, so an uncapped top five covered 3.2 documents on
    average and four benchmark queries lost a relevant document that sat at rank six.
    """
    ranking = [
        _from_document("a1", "doc-a", 1),
        _from_document("a2", "doc-a", 2),
        _from_document("a3", "doc-a", 3),
        _from_document("b1", "doc-b", 4),
        _from_document("c1", "doc-c", 5),
    ]

    capped = cap_per_document(ranking, k=3, max_per_document=2)

    assert [c.chunk_id for c in capped] == ["a1", "a2", "b1"]


def test_the_cap_skips_rather_than_reorders() -> None:
    # What survives is what the ranking already said, minus the repetition. A cap that promoted
    # the next document instead would be a different retriever, not a filter on one.
    ranking = [
        _from_document("a1", "doc-a", 1),
        _from_document("b1", "doc-b", 2),
        _from_document("a2", "doc-a", 3),
        _from_document("a3", "doc-a", 4),
        _from_document("c1", "doc-c", 5),
    ]

    capped = cap_per_document(ranking, k=4, max_per_document=2)

    assert [c.chunk_id for c in capped] == ["a1", "b1", "a2", "c1"]
    assert [c.rank for c in capped] == [1, 2, 3, 4]


def test_two_rather_than_one_per_document() -> None:
    # A runbook symptom and its fix are different chunks and an investigation usually wants
    # both; one per document would trade a document the answer needs for half of one it had.
    ranking = [_from_document("a1", "doc-a", 1), _from_document("a2", "doc-a", 2)]

    assert len(cap_per_document(ranking, k=5, max_per_document=2)) == 2


def test_no_cap_is_the_pool_behaviour() -> None:
    # A candidate pool wants the third chunk: the cap belongs at the end of the pipeline, not in
    # the middle of it, or the reranker never sees what it is meant to judge.
    ranking = [_from_document(f"a{i}", "doc-a", i) for i in range(1, 6)]

    assert len(cap_per_document(ranking, k=5, max_per_document=None)) == 5


async def test_a_capped_retriever_asks_for_more_than_it_returns() -> None:
    """Otherwise the cap returns two chunks when it was asked for five.

    The lexical index is given three chunks of one document and one of another; asking it for
    five and refusing three of them would answer with two.
    """
    def _in(document_id: str, chunk_id: str, content: str) -> StoredChunk:
        return StoredChunk(
            chunk_id=chunk_id,
            document_id=document_id,
            content=content,
            chunk_index=0,
            title=document_id,
            source_type=SourceType.RUNBOOK,
        )

    chunks = [
        _in("doc-pool", "pool-1", "connection pool exhausted maxpoolsize"),
        _in("doc-pool", "pool-2", "connection pool exhausted raise maxpoolsize"),
        _in("doc-pool", "pool-3", "connection pool timeout maxpoolsize"),
        _in("doc-leak", "leak-1", "connection pool mentioned in the memory leak runbook"),
    ]

    index = Bm25Index()
    index.build(chunks)

    result = await Bm25Retriever(index, max_per_document=2).retrieve("connection pool", k=3)

    assert len({c.chunk.document_id for c in result.chunks}) == 2
    assert len(result.chunks) == 3


# ------------------------------------------------ searching for the answer instead ----


class _RoutingEmbeddings(EmbeddingProvider):
    """Gives the hypothesis a different vector from the question, so the store can tell them
    apart."""

    @property
    def model(self) -> str:
        return "fake"

    @property
    def dimensions(self) -> int:
        return 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0, 0.0] if t.startswith("HYPOTHESIS") else [1.0, 0.0, 0.0] for t in texts]

    async def is_available(self) -> bool:
        return True


class _RoutingStore(_FakeStore):
    """Answers the question vector with one document and the hypothesis vector with another."""

    def __init__(self, for_query: list[StoredChunk], for_hypothesis: list[StoredChunk]) -> None:
        super().__init__(for_query)
        self._for_query = for_query
        self._for_hypothesis = for_hypothesis

    async def search(self, embedding, k, filters=None):  # noqa: ANN001, ANN201
        chunks = self._for_hypothesis if embedding[1] == 1.0 else self._for_query

        return [
            RetrievedChunk(chunk=c, score=0.9, rank=rank, retriever="vector")
            for rank, c in enumerate(chunks[:k], start=1)
        ]


class _FakeExpander(QueryExpander):
    def __init__(self, text: str | None = "HYPOTHESIS the passage that would answer it") -> None:
        self._text = text
        self.queries: list[str] = []

    @property
    def name(self) -> str:
        return "hyde"

    async def expand(self, query: str) -> Expansion | None:
        self.queries.append(query)

        if self._text is None:
            return None

        return Expansion(text=self._text, model="fake", prompt_id="hyde_passage.v1", latency_ms=700)


OBSERVABILITY = _stored("observability", "loki stores every line labelled by service_name")


async def test_expansion_reaches_a_document_the_question_alone_does_not() -> None:
    """The measured failure this exists for.

    "how do I query the logs of a service, and what is the label called" never reached the
    observability document, which does not contain the word *label*. The question vector finds
    the wrong thing; the vector of the passage that would answer it finds the right one, and
    fusing the two keeps both.
    """
    retriever = VectorRetriever(
        _RoutingEmbeddings(),
        _RoutingStore(for_query=[LEAK], for_hypothesis=[OBSERVABILITY]),
        expander=_FakeExpander(),
    )

    result = await retriever.retrieve("what is the label called", k=5)

    assert "observability" in [c.chunk_id for c in result.chunks]
    assert result.candidates["hyde"] == ["observability"]
    assert result.candidates["vector"] == ["leak"]


async def test_the_hypothesis_is_fused_rather_than_substituted() -> None:
    # A 3B model asked about an unfamiliar service occasionally writes about a different one.
    # Fusing means that costs a ranking; substituting would cost the answer.
    retriever = VectorRetriever(
        _RoutingEmbeddings(),
        _RoutingStore(for_query=[LEAK], for_hypothesis=[OBSERVABILITY]),
        expander=_FakeExpander(),
    )

    result = await retriever.retrieve("anything", k=5)

    assert {c.chunk_id for c in result.chunks} == {"leak", "observability"}


async def test_an_expanded_retriever_says_so_in_its_name() -> None:
    # A benchmark row named "hybrid" for two different pipelines is a table nobody can act on.
    plain = VectorRetriever(_RoutingEmbeddings(), _RoutingStore([LEAK], [OBSERVABILITY]))
    expanded = VectorRetriever(
        _RoutingEmbeddings(), _RoutingStore([LEAK], [OBSERVABILITY]), expander=_FakeExpander()
    )

    assert plain.name == "vector"
    assert expanded.name == "vector_hyde"

    index = Bm25Index()
    index.build([POOL, LEAK])

    assert HybridRetriever(Bm25Retriever(index), expanded).name == "hybrid_hyde"
    assert HybridRetriever(Bm25Retriever(index), plain).name == "hybrid"


async def test_no_expansion_leaves_the_search_exactly_as_it_was() -> None:
    # The expander returning None is the model being down or unhelpful, and Phase 5 retrieval
    # answered every one of these queries without it.
    retriever = VectorRetriever(
        _RoutingEmbeddings(),
        _RoutingStore(for_query=[LEAK], for_hypothesis=[OBSERVABILITY]),
        expander=_FakeExpander(text=None),
    )

    result = await retriever.retrieve("anything", k=5)

    assert [c.chunk_id for c in result.chunks] == ["leak"]
    assert "hyde" not in result.candidates


async def test_the_expansion_latency_is_reported_separately() -> None:
    # It is a second of a two-second search; a total that hides it makes the dense search look
    # like the thing that got slower.
    retriever = VectorRetriever(
        _RoutingEmbeddings(),
        _RoutingStore([LEAK], [OBSERVABILITY]),
        expander=_FakeExpander(),
    )

    result = await retriever.retrieve("anything", k=5)

    assert result.stage_latency_ms["expand"] == 700
    assert "hyde" in result.stage_latency_ms
