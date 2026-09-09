"""The metrics, and the definitions they encode.

Worth testing precisely because they look obvious. Recall over documents rather than chunks,
precision divided by what was returned rather than by k, a percentile that reports a duration
something actually took — each of those is a decision, and each of them silently changes every
number in the comparison table if it drifts.
"""

from __future__ import annotations

import pytest

from evaluation.metrics import (
    QueryOutcome,
    comparison_table,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    summarize,
)

RUNBOOK = "runbooks/connection-pool-exhaustion.md"
POSTMORTEM = "incidents/INC-00001-orders-pool-exhaustion.md"
UNRELATED = "services/users.md"


def _outcome(
    retrieved: list[str],
    relevant: list[str],
    query_id: str = "Q001",
    retriever: str = "hybrid",
    latency_ms: int = 10,
) -> QueryOutcome:
    return QueryOutcome(
        query_id=query_id,
        query="connection pool exhausted",
        retriever=retriever,
        retrieved=retrieved,
        relevant=relevant,
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------- recall ----


def test_recall_is_the_share_of_the_wanted_documents_that_were_found() -> None:
    assert recall_at_k([RUNBOOK, UNRELATED], [RUNBOOK, POSTMORTEM], k=5) == pytest.approx(0.5)


def test_recall_at_k_only_looks_at_the_first_k() -> None:
    # The context builder spends its budget from the top down; a document at rank 6 is not in
    # the prompt, so it is not found.
    assert recall_at_k([UNRELATED, UNRELATED, UNRELATED, UNRELATED, UNRELATED, RUNBOOK],
                       [RUNBOOK], k=5) == 0.0
    assert recall_at_k([UNRELATED, RUNBOOK], [RUNBOOK], k=5) == 1.0


def test_a_query_that_wants_nothing_is_not_a_division_by_zero() -> None:
    assert recall_at_k([RUNBOOK], [], k=5) == 1.0


# ------------------------------------------------------------------- precision ----


def test_precision_is_divided_by_what_was_returned() -> None:
    """Three results, all three relevant, is precision 1.0.

    Dividing by k would score it 0.6 and call the retriever worse for the corpus being small,
    which is not a property of the retriever.
    """
    assert precision_at_k([RUNBOOK, POSTMORTEM], [RUNBOOK, POSTMORTEM], k=5) == 1.0


def test_precision_counts_the_irrelevant() -> None:
    assert precision_at_k([RUNBOOK, UNRELATED], [RUNBOOK], k=5) == pytest.approx(0.5)


def test_nothing_returned_is_precision_zero() -> None:
    assert precision_at_k([], [RUNBOOK], k=5) == 0.0


# ------------------------------------------------------------------------ mrr ----


def test_reciprocal_rank_rewards_the_first_hit_being_early() -> None:
    """What recall cannot see.

    Two retrievers that both find the runbook inside five score the same recall@5; the one that
    put it first is better.
    """
    assert reciprocal_rank([RUNBOOK, UNRELATED], [RUNBOOK]) == 1.0
    assert reciprocal_rank([UNRELATED, RUNBOOK], [RUNBOOK]) == pytest.approx(0.5)


def test_reciprocal_rank_is_zero_when_nothing_relevant_was_found() -> None:
    assert reciprocal_rank([UNRELATED], [RUNBOOK]) == 0.0


# -------------------------------------------------------------------- summary ----


def test_the_summary_averages_over_the_query_set() -> None:
    scores = summarize(
        [
            _outcome([RUNBOOK], [RUNBOOK], query_id="Q001"),
            _outcome([UNRELATED], [RUNBOOK], query_id="Q002"),
        ]
    )

    assert scores.queries == 2
    assert scores.recall_at_5 == pytest.approx(0.5)
    assert scores.mrr == pytest.approx(0.5)


def test_the_summary_names_what_was_never_found() -> None:
    # The most useful output of an evaluation is not the average, it is the list of what the
    # retriever cannot find.
    scores = summarize(
        [
            _outcome([RUNBOOK], [RUNBOOK], query_id="Q001"),
            _outcome([UNRELATED], [RUNBOOK], query_id="Q002"),
        ]
    )

    assert scores.misses == ["Q002"]


def test_mixing_retrievers_in_one_summary_is_refused() -> None:
    # Silently averaging two retrievers produces a plausible number for a thing that does not
    # exist, which is the worst failure mode a benchmark has.
    with pytest.raises(ValueError, match="mix"):
        summarize(
            [
                _outcome([RUNBOOK], [RUNBOOK], retriever="bm25"),
                _outcome([RUNBOOK], [RUNBOOK], retriever="vector"),
            ]
        )


def test_summarising_nothing_is_refused() -> None:
    with pytest.raises(ValueError):
        summarize([])


def test_latency_percentiles_report_a_duration_something_took() -> None:
    scores = summarize(
        [
            _outcome([RUNBOOK], [RUNBOOK], query_id=f"Q{i}", latency_ms=latency)
            for i, latency in enumerate([10, 20, 30, 40, 900], start=1)
        ]
    )

    assert scores.latency_p50_ms == 30
    assert scores.latency_p95_ms == 900


# ---------------------------------------------------------------------- table ----


def test_the_table_is_ordered_by_the_question_it_answers() -> None:
    """Best recall@5 first, because the table exists to say which retriever to use."""
    weak = summarize([_outcome([UNRELATED], [RUNBOOK], retriever="bm25")])
    strong = summarize([_outcome([RUNBOOK], [RUNBOOK], retriever="hybrid_rerank")])

    lines = comparison_table([weak, strong]).splitlines()

    assert lines[2].startswith("| hybrid_rerank")
    assert lines[3].startswith("| bm25")
