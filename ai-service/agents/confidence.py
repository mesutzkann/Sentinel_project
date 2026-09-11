"""How sure the agent is, computed rather than asked for.

docs/planning.md §7 gives the formula:

    confidence = 0.45 * evidence_support        # weighted evidence ratio, diversity bonus
               + 0.25 * validator_confidence
               + 0.15 * historical_similarity   # similarity to the nearest past incident
               + 0.15 * hypothesis_margin       # gap between the first and second hypothesis

**The model never sets this number.** It is asked for an explanation and for a verdict; the score
is arithmetic over things that were measured — what evidence was cited, out of what was
collected, across how many independent sources, and by how much the winning explanation beat the
runner-up. A language model's stated confidence is a fluent guess, and the one number a human uses
to decide whether to trust the rest should not be one.

**A term that cannot be measured is dropped, not zeroed.** ``historical_similarity`` needs the
incident-embedding similarity search that arrives in Phase 9, and ``hypothesis_margin`` is
undefined when the agent produced a single hypothesis. Writing 0.0 for either would look like a
measurement — "we checked, and it was nothing" — and would depress every score by that term's
weight: with the historical term missing, the 0.70 threshold would really behave like 0.85 and
almost no run would clear it. So the available terms are renormalised to sum to 1, and the score
carries which terms it did not have. A confidence of 0.82 from three terms is a different claim
from 0.82 from four, and the output has to be able to tell a human that.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from agents.context import EvidenceItem, Hypothesis

# The four weights, exactly as the planning document states them. They are not renormalised here:
# that happens per run, over whichever terms the run could measure.
WEIGHT_EVIDENCE_SUPPORT = 0.45
WEIGHT_VALIDATOR_CONFIDENCE = 0.25
WEIGHT_HISTORICAL_SIMILARITY = 0.15
WEIGHT_HYPOTHESIS_MARGIN = 0.15

# Below this the agent stops and asks for a human, and produces no recommendation. From
# docs/planning.md §7.
CONFIDENCE_THRESHOLD = 0.70

# How evidence support splits between "how much of the evidence backs this" and "how many
# independent kinds of signal back it". Two thirds and one third, because a conclusion resting on
# most of the evidence from a single source is weaker than the ratio alone suggests, and one
# resting on a little evidence from four sources is stronger. Both halves are needed: diversity
# alone would let four thin facts outrank one decisive measurement.
SUPPORT_WEIGHT_SHARE = 0.7
SUPPORT_DIVERSITY_SHARE = 0.3

# The cited weight at which a conclusion counts as fully supported, whatever else was collected.
#
# **This is a correction to the planning document's "weighted evidence ratio", and it was
# measured rather than reasoned into.** As a plain ratio of cited weight to collected weight, the
# term punishes thoroughness: a run that collects six facts and rests on the two decisive ones
# scores 0.43 where a run that collected only those two scores 1.0, for the same conclusion drawn
# from the same measurements. Against the pool-exhaustion evidence with qwen2.5:3b-instruct, the
# correct root cause — pool saturated, timeouts in the logs, the commit that lowered the cap —
# scored 0.59 confidence and stopped for a human, and the demo scenario collects thirteen facts
# rather than six, which would have made it worse. A threshold no correct answer can reach is not
# a threshold.
#
# So the denominator saturates. Two decisive facts (a 0.9 measurement and a 0.8 positive finding)
# are enough to fully support a conclusion; a third does not make it more true. Below that much
# collected evidence the denominator is simply everything there was, so a thin run still cannot
# reach 1.0 by citing its own single fact.
SUFFICIENT_CITED_WEIGHT = 2.0


@dataclass(frozen=True, slots=True)
class ConfidenceTerm:
    """One input to the score, with what it was worth and whether it existed at all."""

    name: str
    weight: float
    value: float | None

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def contribution(self) -> float:
        """The unnormalised product. Zero for a missing term, which is why it is not the score."""
        return self.weight * (self.value or 0.0)


@dataclass(frozen=True, slots=True)
class ConfidenceScore:
    """A number between 0 and 1, and the arithmetic that produced it."""

    value: float
    terms: tuple[ConfidenceTerm, ...]

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(term.name for term in self.terms if not term.available)

    @property
    def measured(self) -> tuple[ConfidenceTerm, ...]:
        return tuple(term for term in self.terms if term.available)

    @property
    def meets_threshold(self) -> bool:
        return self.value >= CONFIDENCE_THRESHOLD

    def explain(self) -> str:
        """One line for the timeline, including what was not measured.

        The missing terms are named in the sentence rather than left to a payload field, because
        the sentence is what a human reads and "0.81, historical_similarity not available" is the
        whole of the caveat.
        """
        parts = [f"{term.name}={term.value:.2f}" for term in self.measured]
        line = f"confidence {self.value:.2f} from {', '.join(parts)}"

        if self.missing:
            line = f"{line}; not available: {', '.join(self.missing)}"

        return line

    def to_payload(self) -> dict[str, Any]:
        """The breakdown as it crosses the callback boundary.

        Every term is present, missing ones as ``null``. A payload that omitted them would make a
        run from today and a Phase 9 run indistinguishable in the database.
        """
        return {
            "value": round(self.value, 4),
            "threshold": CONFIDENCE_THRESHOLD,
            "meets_threshold": self.meets_threshold,
            "terms": {
                term.name: {
                    "weight": term.weight,
                    "value": None if term.value is None else round(term.value, 4),
                }
                for term in self.terms
            },
            "missing_terms": list(self.missing),
        }


def score_confidence(
    *,
    evidence_support: float,
    validator_confidence: float | None = None,
    historical_similarity: float | None = None,
    hypothesis_margin: float | None = None,
) -> ConfidenceScore:
    """Combine whatever terms this run could measure.

    ``evidence_support`` is not optional: an investigation always knows what it cited out of what
    it collected, and a run with no evidence at all *measures* that as 0.0 rather than as an
    absence. The other three are ``None`` when they were not available.
    """
    terms = (
        ConfidenceTerm("evidence_support", WEIGHT_EVIDENCE_SUPPORT, _clamp(evidence_support)),
        ConfidenceTerm(
            "validator_confidence", WEIGHT_VALIDATOR_CONFIDENCE, _clamp(validator_confidence)
        ),
        ConfidenceTerm(
            "historical_similarity", WEIGHT_HISTORICAL_SIMILARITY, _clamp(historical_similarity)
        ),
        ConfidenceTerm("hypothesis_margin", WEIGHT_HYPOTHESIS_MARGIN, _clamp(hypothesis_margin)),
    )

    divisor = sum(term.weight for term in terms if term.available)

    if divisor == 0.0:
        # Unreachable while evidence_support is required, and handled anyway: a score that
        # divided by zero would raise inside the node that decides whether to trust the run.
        return ConfidenceScore(value=0.0, terms=terms)

    total = sum(term.contribution for term in terms if term.available)

    return ConfidenceScore(value=_clamped(total / divisor), terms=terms)


def evidence_support(evidence: Sequence[EvidenceItem], cited: Iterable[int]) -> float:
    """How much of what was collected backs a claim, and from how many kinds of source.

    The weighted ratio is the planning document's term, with the saturating denominator described
    at :data:`SUFFICIENT_CITED_WEIGHT`; the diversity share is its parenthesised "kaynak
    çeşitliliği bonusu", made explicit. Diversity is measured against the sources this run
    actually collected rather than against all nine, because an investigation cannot cite a kind
    of signal nobody gathered and should not be marked down for it.

    **The known bias, stated rather than defended against here.** A hypothesis that cites every
    piece of evidence scores 1.0 whether or not the evidence supports it, and nothing in this
    function can tell the difference — it sees indices, not meaning. That is the critic's job:
    VALIDATE is asked precisely whether the cited evidence supports the claim, and its verdict is
    a quarter of the score. Guessing at relevance from weights here would put the diagnosis
    somewhere invisible again.
    """
    items = list(evidence)

    if not items:
        return 0.0

    indices = {index for index in cited if 0 <= index < len(items)}

    if not indices:
        return 0.0

    total_weight = sum(item.weight for item in items)
    cited_weight = sum(items[index].weight for index in indices)

    # Whichever is smaller: everything there was to cite, or enough to be sure. Never zero here,
    # because an item with weight 0 across the whole list is the only way to reach it and the
    # guard above has already returned for an empty list.
    denominator = min(total_weight, SUFFICIENT_CITED_WEIGHT)

    # Clamped on its own rather than only at the end: citing more than enough is still enough,
    # and letting the overflow spill over would buy back the diversity half of the score.
    weight_ratio = _clamped(cited_weight / denominator) if denominator else 0.0

    sources = {item.source for item in items}
    cited_sources = {items[index].source for index in indices}
    diversity_ratio = len(cited_sources) / len(sources) if sources else 0.0

    return _clamped(
        SUPPORT_WEIGHT_SHARE * weight_ratio + SUPPORT_DIVERSITY_SHARE * diversity_ratio
    )


def hypothesis_margin(hypotheses: Sequence[Hypothesis]) -> float | None:
    """The gap between the winner and the best hypothesis that is a *different* claim.

    A single hypothesis has no margin, and it is tempting to call that 1.0 — nothing competed, so
    nothing disagreed. It is the opposite: a run that produced one explanation never considered an
    alternative, and this term exists to measure that the winner beat one. So it is unavailable
    and the score is renormalised without it, which says "not measured" rather than "perfect" or
    "zero".

    **A paraphrase is not an alternative, and measuring against one punished the agent for
    repeating itself.** On the pool-exhaustion scenario the 3B proposed "the database connection
    pool is exhausted" at 0.797 and "the connection pool is being used beyond its capacity" at
    0.745 — one claim, written twice, citing the same two facts. The gap between them was 0.05,
    and a correct conclusion the critic had just accepted at 0.95 scored 0.667 and stopped for a
    human. With the runner-up taken as the best hypothesis resting on a *different* set of facts,
    the same run scores above the threshold and finishes.

    Sameness is judged by the cited evidence, because that is what this module can see. Two
    hypotheses citing exactly the same facts are the same claim as far as any arithmetic over
    evidence is concerned, whatever they say in prose. It is a deliberately crude test: a
    hypothesis citing a subset of another's facts counts as different, and telling those apart
    would need the meaning of the sentences, which is the one thing not to put in here.
    """
    ranked = sorted(hypotheses, key=lambda h: h.score, reverse=True)

    if not ranked:
        return None

    winner, cited = ranked[0], frozenset(ranked[0].supporting_evidence)

    for other in ranked[1:]:
        if frozenset(other.supporting_evidence) != cited:
            return _clamped(winner.score - other.score)

    # Every other hypothesis rests on exactly the same facts, so nothing here competed with the
    # winner — the same case as having produced one hypothesis, and reported the same way.
    return None


def _clamp(value: float | None) -> float | None:
    """Hold a term inside [0, 1], keeping ``None`` distinct from 0.0."""
    if value is None:
        return None

    return _clamped(value)


def _clamped(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
