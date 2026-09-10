"""The confidence score: the renormalisation, and what each term actually measures.

The renormalisation is the part worth testing hardest. It is the difference between a threshold
of 0.70 that means what it says and one that behaves like 0.85 because a term nobody can measure
yet is being written down as a zero.
"""

from __future__ import annotations

import pytest

from agents.confidence import (
    CONFIDENCE_THRESHOLD,
    WEIGHT_EVIDENCE_SUPPORT,
    WEIGHT_HISTORICAL_SIMILARITY,
    WEIGHT_HYPOTHESIS_MARGIN,
    WEIGHT_VALIDATOR_CONFIDENCE,
    evidence_support,
    hypothesis_margin,
    score_confidence,
)
from agents.context import EvidenceItem, EvidenceSource, Hypothesis
from tests.support import pool_evidence


def test_the_four_weights_are_the_planning_documents() -> None:
    total = (
        WEIGHT_EVIDENCE_SUPPORT
        + WEIGHT_VALIDATOR_CONFIDENCE
        + WEIGHT_HISTORICAL_SIMILARITY
        + WEIGHT_HYPOTHESIS_MARGIN
    )

    assert total == pytest.approx(1.0)


def test_all_four_terms_is_the_plain_weighted_sum() -> None:
    score = score_confidence(
        evidence_support=0.8,
        validator_confidence=0.6,
        historical_similarity=0.4,
        hypothesis_margin=0.2,
    )

    expected = 0.45 * 0.8 + 0.25 * 0.6 + 0.15 * 0.4 + 0.15 * 0.2

    assert score.value == pytest.approx(expected)
    assert score.missing == ()


def test_a_missing_term_is_renormalised_rather_than_zeroed() -> None:
    """Three perfect terms out of four is 1.0, not 0.85.

    This is the whole reason the score is not a plain weighted sum: `historical_similarity` needs
    Phase 9, and treating "not measured" as "measured zero" would put a ceiling of 0.85 over
    every investigation the system currently runs.
    """
    score = score_confidence(
        evidence_support=1.0,
        validator_confidence=1.0,
        hypothesis_margin=1.0,
    )

    assert score.value == pytest.approx(1.0)
    assert score.missing == ("historical_similarity",)


def test_the_surviving_terms_keep_their_ratios() -> None:
    score = score_confidence(evidence_support=1.0, validator_confidence=0.0)

    # 0.45 and 0.25 of a 0.70 divisor: the evidence term is still nine fourteenths of the answer.
    assert score.value == pytest.approx(0.45 / 0.70)
    assert set(score.missing) == {"historical_similarity", "hypothesis_margin"}


def test_evidence_support_alone_still_produces_a_score() -> None:
    score = score_confidence(evidence_support=0.5)

    assert score.value == pytest.approx(0.5)
    assert len(score.measured) == 1


def test_no_evidence_support_is_measured_as_zero_not_as_missing() -> None:
    """A run that cited nothing measured that. It is the one term that is never unavailable."""
    score = score_confidence(evidence_support=0.0, validator_confidence=1.0)

    assert "evidence_support" not in score.missing
    assert score.value == pytest.approx(0.25 / 0.70)


def test_the_threshold_is_inclusive_and_explains_itself() -> None:
    score = score_confidence(evidence_support=CONFIDENCE_THRESHOLD)

    assert score.meets_threshold is True
    assert "evidence_support=0.70" in score.explain()
    assert "historical_similarity" in score.explain()


def test_the_payload_keeps_missing_terms_as_null() -> None:
    """An old run and a Phase 9 run must not look the same in the database."""
    payload = score_confidence(evidence_support=0.8, validator_confidence=0.9).to_payload()

    assert payload["terms"]["historical_similarity"]["value"] is None
    assert payload["terms"]["evidence_support"]["value"] == 0.8
    assert payload["missing_terms"] == ["historical_similarity", "hypothesis_margin"]
    assert payload["threshold"] == CONFIDENCE_THRESHOLD


def test_terms_outside_zero_to_one_are_clamped() -> None:
    assert score_confidence(evidence_support=2.0).value == pytest.approx(1.0)
    assert score_confidence(evidence_support=-1.0).value == pytest.approx(0.0)


# ---- evidence support --------------------------------------------------------------


def test_citing_everything_scores_one_and_citing_nothing_scores_zero() -> None:
    evidence = pool_evidence()

    assert evidence_support(evidence, [0, 1, 2]) == pytest.approx(1.0)
    assert evidence_support(evidence, []) == pytest.approx(0.0)


def test_support_pays_for_agreement_across_sources() -> None:
    """Two facts of equal weight, one source or two. The second is worth more."""
    same_source = [
        EvidenceItem(source=EvidenceSource.LOGS, summary="a", weight=0.5),
        EvidenceItem(source=EvidenceSource.LOGS, summary="b", weight=0.5),
        EvidenceItem(source=EvidenceSource.METRICS, summary="c", weight=0.5),
        EvidenceItem(source=EvidenceSource.DATABASE, summary="d", weight=0.5),
    ]

    one_kind = evidence_support(same_source, [0, 1])
    two_kinds = evidence_support(same_source, [0, 2])

    assert two_kinds > one_kind


def test_a_thorough_run_is_not_punished_for_collecting_more() -> None:
    """The same two decisive facts support the same conclusion however much else was gathered.

    As a plain ratio this was the bug that made the threshold unreachable: with six facts
    collected, the correct pool-exhaustion conclusion scored 0.59 and stopped for a human. See
    SUFFICIENT_CITED_WEIGHT.
    """
    decisive = pool_evidence()[:2]
    padded = [
        *decisive,
        *[
            EvidenceItem(source=EvidenceSource.TRACES, summary=f"a trace fact {n}", weight=0.6)
            for n in range(5)
        ],
    ]

    support = evidence_support(padded, [0, 1])

    # Coverage saturates at 1.7 of a 2.0 denominator; only the diversity half falls, from two of
    # two sources to two of three. As a plain ratio of cited weight to collected weight the
    # coverage half would have been 1.7/4.7, and the whole score would sit at 0.45.
    assert support == pytest.approx(0.7 * (1.7 / 2.0) + 0.3 * (2 / 3))
    assert support > 0.75
    assert evidence_support(decisive, [0, 1]) == pytest.approx(1.0)


def test_citing_more_than_enough_does_not_buy_back_missing_diversity() -> None:
    one_source = [
        EvidenceItem(source=EvidenceSource.LOGS, summary=f"fact {n}", weight=0.9) for n in range(6)
    ]
    one_source.append(EvidenceItem(source=EvidenceSource.METRICS, summary="a gauge", weight=0.6))

    # Four 0.9 facts is well past the saturation point, and the score still stops short of 1.0
    # because only one of the two kinds of signal collected agrees.
    assert evidence_support(one_source, [0, 1, 2, 3]) == pytest.approx(0.7 + 0.3 * 0.5)


def test_citations_outside_the_evidence_are_ignored() -> None:
    """A model that cites [9] when there are three facts has not supported anything."""
    evidence = pool_evidence()

    assert evidence_support(evidence, [9, -1]) == pytest.approx(0.0)
    assert evidence_support(evidence, [0, 9]) == pytest.approx(evidence_support(evidence, [0]))


def test_a_repeated_citation_is_counted_once() -> None:
    evidence = pool_evidence()

    assert evidence_support(evidence, [0, 0, 0]) == pytest.approx(evidence_support(evidence, [0]))


def test_support_with_no_evidence_at_all_is_zero() -> None:
    assert evidence_support([], [0]) == pytest.approx(0.0)


# ---- margin ------------------------------------------------------------------------


def test_a_single_hypothesis_has_no_margin() -> None:
    """Not 1.0 and not 0.0: a run that considered one explanation measured nothing here."""
    assert hypothesis_margin([Hypothesis(title="only one", score=0.9)]) is None


def test_the_margin_is_the_gap_between_the_first_two() -> None:
    hypotheses = [
        Hypothesis(title="second", score=0.4),
        Hypothesis(title="first", score=0.75),
        Hypothesis(title="third", score=0.1),
    ]

    assert hypothesis_margin(hypotheses) == pytest.approx(0.35)


def test_no_hypotheses_has_no_margin() -> None:
    assert hypothesis_margin([]) is None
