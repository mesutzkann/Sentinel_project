"""The cross-encoder, with a stand-in for the model.

The model itself is not tested here and could not be: 2.2 GB of weights whose scores are the
thing under test elsewhere. What is tested is everything around it — that scores become an
order, that the order is deterministic, that a missing dependency is reported in a way somebody
can act on, and that the score written to the evidence panel is on a scale a person can read.
"""

from __future__ import annotations

from typing import Any

import pytest

from rag.documents import RetrievedChunk, SourceType, StoredChunk
from rag.rerank import (
    CrossEncoderReranker,
    RerankError,
    RerankUnavailableError,
    order,
)


def _candidates(*chunk_ids: str) -> list[RetrievedChunk]:
    """Candidates as the hybrid retriever hands them over: ranked, best first."""
    return [
        RetrievedChunk(
            chunk=StoredChunk(
                chunk_id=chunk_id,
                document_id=f"doc-{chunk_id}",
                content="content",
                chunk_index=0,
                title=chunk_id,
                source_type=SourceType.RUNBOOK,
            ),
            score=1.0 / rank,
            rank=rank,
            retriever="hybrid",
        )
        for rank, chunk_id in enumerate(chunk_ids, start=1)
    ]


class _FakeEncoder:
    """Returns prepared scores, in the order the pairs arrive."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.calls: list[dict[str, Any]] = []

    def predict(self, pairs: list[tuple[str, str]], /, **kwargs: Any) -> list[float]:
        self.calls.append({"pairs": pairs, **kwargs})

        return self._scores[: len(pairs)]


# ------------------------------------------------------------------- ordering ----


async def test_the_reranker_overrides_the_order_it_was_given() -> None:
    """The entire point: fusion proposes, the cross-encoder decides.

    `c` arrived third and is the most relevant. A reranker that respected the incoming order
    would be an expensive no-op.
    """
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([0.1, 0.2, 0.9]))

    result = await reranker.rerank("query", _candidates("a", "b", "c"), k=3)

    assert [r.chunk_id for r in result] == ["c", "b", "a"]
    assert [r.rank for r in result] == [1, 2, 3]


async def test_only_k_survive() -> None:
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([0.1, 0.9, 0.5]))

    result = await reranker.rerank("query", _candidates("a", "b", "c"), k=2)

    assert [r.chunk_id for r in result] == ["b", "c"]


async def test_the_deciding_stage_is_named_on_the_chunk() -> None:
    # An evidence panel that says a chunk came from "hybrid" when a cross-encoder chose it is
    # attributing the decision to the wrong stage.
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([0.9, 0.1]))

    result = await reranker.rerank("query", _candidates("a", "b"), k=2)

    assert all(r.retriever == "cross_encoder" for r in result)


def test_ties_break_on_the_incoming_rank() -> None:
    """Same scores, same candidates, same order — twice.

    Without it the benchmark measures the sort. All three score identically, so the order fusion
    proposed survives.
    """
    first = order(_candidates("a", "b", "c"), [0.5, 0.5, 0.5], k=3, retriever="cross_encoder")
    second = order(_candidates("a", "b", "c"), [0.5, 0.5, 0.5], k=3, retriever="cross_encoder")

    assert [r.chunk_id for r in first] == ["a", "b", "c"]
    assert [r.chunk_id for r in second] == ["a", "b", "c"]


async def test_nothing_in_nothing_out() -> None:
    # An empty candidate list is what a narrowly filtered search returns, and loading 2.2 GB of
    # weights to score zero pairs would be a memorable way to answer it.
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([]))

    assert await reranker.rerank("query", [], k=5) == []


# --------------------------------------------------------------------- scores ----


async def test_logits_are_squashed_to_a_readable_range() -> None:
    """Raw cross-encoder output is a logit, and the number ends up in a UI column.

    sentence-transformers applies the sigmoid on some versions and not others, so the scale is
    settled here rather than assumed.
    """
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([6.3, -4.1]))

    result = await reranker.rerank("query", _candidates("a", "b"), k=2)

    assert 0.9 < result[0].score < 1.0
    assert 0.0 < result[1].score < 0.1


async def test_scores_already_in_range_are_left_alone() -> None:
    # Squashing twice compresses everything into a narrow band around 0.5. It ranks the same and
    # reads as though the model were uncertain about everything.
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([0.92, 0.04]))

    result = await reranker.rerank("query", _candidates("a", "b"), k=2)

    assert result[0].score == pytest.approx(0.92)


async def test_a_score_per_candidate_or_an_error() -> None:
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([0.9]))

    with pytest.raises(RerankError, match="got 1"):
        await reranker.rerank("query", _candidates("a", "b"), k=2)


async def test_scores_that_are_not_numbers_are_an_error() -> None:
    class _Nonsense:
        def predict(self, pairs: list[tuple[str, str]], /, **kwargs: Any) -> list[str]:
            return ["very relevant"] * len(pairs)

    reranker = CrossEncoderReranker(encoder=_Nonsense())

    with pytest.raises(RerankError):
        await reranker.rerank("query", _candidates("a"), k=1)


# ------------------------------------------------------------------ the model ----


async def test_the_query_is_paired_with_every_candidate() -> None:
    encoder = _FakeEncoder([0.5, 0.5])
    reranker = CrossEncoderReranker(encoder=encoder, batch_size=8)

    await reranker.rerank("why is orders slow", _candidates("a", "b"), k=2)

    assert encoder.calls[0]["pairs"] == [
        ("why is orders slow", "content"),
        ("why is orders slow", "content"),
    ]
    assert encoder.calls[0]["batch_size"] == 8


async def test_a_model_that_cannot_be_loaded_says_what_to_do_about_it() -> None:
    """The realistic failure, and the one whose message has to be actionable.

    The cross-encoder is an optional dependency (ADR-0005), so a fresh checkout has neither
    torch nor the weights. Both spellings of the failure — library missing, model missing —
    arrive as the same exception type and both name the thing to install.
    """
    reranker = CrossEncoderReranker(model="sentinel/not-a-real-model")

    with pytest.raises(RerankUnavailableError, match="rerank"):
        await reranker.rerank("query", _candidates("a"), k=1)

    assert reranker.unavailable_reason


async def test_availability_is_reported_rather_than_raised() -> None:
    # Called from GET /rag/stats, which must answer even when nothing is installed.
    reranker = CrossEncoderReranker(model="sentinel/not-a-real-model")

    assert await reranker.is_available() is False


async def test_an_encoder_that_was_handed_in_is_available() -> None:
    reranker = CrossEncoderReranker(encoder=_FakeEncoder([]))

    assert await reranker.is_available() is True
    assert reranker.unavailable_reason is None
