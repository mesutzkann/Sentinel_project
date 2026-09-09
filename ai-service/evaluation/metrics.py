"""Ranking metrics, and the definitions they are computed under.

Three numbers, and the reason each is here:

*Recall@k* — of the documents that should have been found, how many were, in the top k. It is
the metric that matters most for this system, because the agent does not read the ranking: it
reads the context block built from it. A relevant runbook at rank 4 is as good as one at rank 1
and one at rank 6 is invisible.

*MRR* — one over the rank of the first relevant document. It is what recall cannot see: two
retrievers that both find the runbook inside five have the same recall@5, and the one that put
it first is better, because the context builder spends its budget from the top down.

*Success@k* — did anything relevant appear in the top k at all. The question "is the first
result right?", which recall cannot answer here: a query with two relevant documents caps
recall@1 at 0.5 however good the retriever is, so a recall@1 of 0.65 over this query set is
much closer to its ceiling (0.78, given the labels) than the number looks. Success@1 says the
same thing without the ceiling in the way.

*Precision@k* — of what was returned, how much was relevant. Low precision is not fatal here
(an irrelevant chunk costs tokens, not correctness) but a collapse in it is the signature of a
retriever matching on something incidental, and it is the number that catches a lexical index
tokenising badly.

**Everything is computed over documents, not chunks.** A query's ground truth is a set of
document keys, because that is what a human can label and check: "this question is answered by
`runbooks/connection-pool-exhaustion.md`" is a statement about the corpus that stays true when
the chunker changes. Chunk ids are generated at ingest and change when the chunker's target
size does, which would make last week's ground truth silently wrong rather than obviously stale.

So a run retrieves ``k`` *chunks*, maps them to the documents they came from — in rank order,
duplicates dropped — and scores that list. Recall@5 therefore reads as: of the documents that
should be found, how many are cited by the five chunks the retriever would have put in the
prompt.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    """One query, through one retriever."""

    query_id: str
    query: str
    retriever: str
    # Document keys in rank order, deduplicated.
    retrieved: list[str]
    relevant: list[str]
    latency_ms: int = 0

    @property
    def hit_rank(self) -> int | None:
        """Position of the first relevant document, 1-based. ``None`` if it was never found."""
        for position, key in enumerate(self.retrieved, start=1):
            if key in self.relevant:
                return position

        return None


@dataclass(frozen=True, slots=True)
class RetrieverScores:
    """What one retriever scored over the whole query set."""

    retriever: str
    queries: int
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    success_at_1: float
    success_at_3: float
    success_at_5: float
    mrr: float
    precision_at_5: float
    latency_p50_ms: int
    latency_p95_ms: int
    # Query ids where nothing relevant appeared in the top 5. The most useful output of an
    # evaluation is not the average, it is the list of what the retriever cannot find.
    misses: list[str] = field(default_factory=list)

    def as_row(self) -> str:
        """One line of the comparison table."""
        return (
            f"| {self.retriever} | {self.recall_at_1:.3f} | {self.recall_at_3:.3f} "
            f"| {self.recall_at_5:.3f} | {self.success_at_1:.3f} | {self.success_at_5:.3f} "
            f"| {self.mrr:.3f} | {self.precision_at_5:.3f} "
            f"| {self.latency_p50_ms} | {self.latency_p95_ms} | {len(self.misses)} |"
        )


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the relevant documents that appear in the first ``k`` results.

    A query with no relevant documents scores 1.0 rather than 0.0 — it asked for nothing and got
    it. The eval loader rejects such queries anyway; the definition is here so that the function
    does not divide by zero in some later caller that does not.
    """
    wanted = set(relevant)

    if not wanted:
        return 1.0

    found = wanted.intersection(retrieved[:k])

    return len(found) / len(wanted)


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of the first ``k`` results that are relevant.

    Divided by the number actually returned, not by ``k``. A retriever that returns three
    documents and gets all three right has precision 1.0; scoring it 0.6 would penalise it for
    the corpus being small, which is not a property of the retriever.
    """
    top = retrieved[:k]

    if not top:
        return 0.0

    wanted = set(relevant)

    return sum(1 for key in top if key in wanted) / len(top)


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """One over the position of the first relevant document; 0.0 if there is none."""
    wanted = set(relevant)

    for position, key in enumerate(retrieved, start=1):
        if key in wanted:
            return 1.0 / position

    return 0.0


def success_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """1.0 if anything relevant is in the first ``k`` results, else 0.0.

    Averaged over a query set this is the hit rate — the share of questions the retrieval
    answered at all. It is the number to quote when someone asks how often retrieval is right,
    and the number recall is mistaken for.
    """
    wanted = set(relevant)

    return 1.0 if wanted.intersection(retrieved[:k]) else 0.0


def recall_ceiling(outcomes: list[QueryOutcome]) -> float:
    """The highest recall@1 these labels allow.

    A query with two relevant documents cannot score above 0.5 at k=1, so the mean of
    ``1 / len(relevant)`` is the ceiling for the set. Reported next to recall@1 because a number
    without its maximum is not a measurement.
    """
    if not outcomes:
        return 0.0

    return _mean(1.0 / len(o.relevant) for o in outcomes if o.relevant)


def summarize(outcomes: list[QueryOutcome]) -> RetrieverScores:
    """Average the per-query numbers into the row that goes in the comparison table."""
    if not outcomes:
        raise ValueError("Cannot summarize an empty set of outcomes.")

    retrievers = {outcome.retriever for outcome in outcomes}

    if len(retrievers) > 1:
        # Silently averaging two retrievers together produces a plausible number for a thing
        # that does not exist, which is the worst possible failure mode for a benchmark.
        raise ValueError(f"Outcomes mix several retrievers: {', '.join(sorted(retrievers))}")

    latencies = sorted(outcome.latency_ms for outcome in outcomes)

    return RetrieverScores(
        retriever=outcomes[0].retriever,
        queries=len(outcomes),
        recall_at_1=_mean(recall_at_k(o.retrieved, o.relevant, 1) for o in outcomes),
        recall_at_3=_mean(recall_at_k(o.retrieved, o.relevant, 3) for o in outcomes),
        recall_at_5=_mean(recall_at_k(o.retrieved, o.relevant, 5) for o in outcomes),
        success_at_1=_mean(success_at_k(o.retrieved, o.relevant, 1) for o in outcomes),
        success_at_3=_mean(success_at_k(o.retrieved, o.relevant, 3) for o in outcomes),
        success_at_5=_mean(success_at_k(o.retrieved, o.relevant, 5) for o in outcomes),
        mrr=_mean(reciprocal_rank(o.retrieved, o.relevant) for o in outcomes),
        precision_at_5=_mean(precision_at_k(o.retrieved, o.relevant, 5) for o in outcomes),
        latency_p50_ms=_percentile(latencies, 50),
        latency_p95_ms=_percentile(latencies, 95),
        misses=[o.query_id for o in outcomes if recall_at_k(o.retrieved, o.relevant, 5) == 0.0],
    )


def comparison_table(scores: list[RetrieverScores]) -> str:
    """Every measured retriever side by side, as Markdown.

    Ordered by recall@5 rather than by the order they were run, because the question the table
    answers is which one to use.
    """
    header = (
        "| retriever | R@1 | R@3 | R@5 | S@1 | S@5 | MRR | P@5 | p50 ms | p95 ms | misses |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|"
    )
    rows = [s.as_row() for s in sorted(scores, key=lambda s: -s.recall_at_5)]

    return "\n".join([header, *rows])


def _mean(values: Iterable[float]) -> float:
    collected = list(values)

    return sum(collected) / len(collected) if collected else 0.0


def _percentile(sorted_values: list[int], percentile: int) -> int:
    """Nearest-rank percentile over an already sorted list.

    Nearest-rank rather than interpolated: these are latencies of individual searches, and an
    interpolated p95 reports a duration that no search actually took.
    """
    if not sorted_values:
        return 0

    if len(sorted_values) == 1:
        return sorted_values[0]

    index = math.ceil(percentile / 100 * len(sorted_values)) - 1

    return sorted_values[max(0, min(index, len(sorted_values) - 1))]
