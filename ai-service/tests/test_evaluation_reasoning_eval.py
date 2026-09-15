"""The agent-model benchmark's loader, its scoring, and the fixtures themselves.

Two different things again. The scoring is code: "correct" and "false rejection" are the two
numbers the demo default gets chosen on, and a benchmark that computed either one loosely would
be worse than not running.

The rest is a test of data. The fixtures name scenario codes and evidence sources that exist
elsewhere in the repository — `ScenarioCategory`, `EvidenceSource`, and the scenario list in
`sample-services/chaos/scenarios.md` — and a code renamed on one side turns every case into a
guaranteed miss that reads like a model regression.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.categories import ScenarioCategory
from agents.states import State
from evaluation.reasoning_eval import (
    DEFAULT_CASES,
    CaseOutcome,
    EvalError,
    load_cases,
    score,
)


def _outcome(**overrides: object) -> CaseOutcome:
    fields: dict[str, object] = {
        "case_id": "R01",
        "model": "scripted",
        "expected_category": ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION,
    }

    return CaseOutcome(**{**fields, **overrides})  # type: ignore[arg-type]


# ---- the fixtures ------------------------------------------------------------------


def test_the_shipped_cases_load() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 7
    assert {case.id for case in cases} == {"R01", "R02", "R03", "R04", "R05", "R06", "R07"}


def test_every_expected_category_is_a_real_scenario_code() -> None:
    """A code renamed in ChaosCodes.cs and not here scores every run as a miss."""
    for case in load_cases(DEFAULT_CASES):
        assert case.expected_category in set(ScenarioCategory)
        assert case.scenario == case.expected_category


def test_every_case_carries_evidence_from_more_than_one_source() -> None:
    """Single-source evidence would cap the diversity half of the score for every model alike."""
    for case in load_cases(DEFAULT_CASES):
        assert len({item.source for item in case.evidence}) >= 3
        assert len(case.evidence) >= 5


def test_every_case_states_its_discriminator() -> None:
    """The signal that separates a scenario from its nearest neighbour has to be in the case."""
    for case in load_cases(DEFAULT_CASES):
        assert case.discriminator


def test_a_case_with_an_unknown_source_is_refused(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text(
        json.dumps(
            {
                "id": "R99",
                "scenario": "DB_DEADLOCK",
                "expected_category": "DB_DEADLOCK",
                "incident_code": "INC-1",
                "service": "payments",
                "query": "why",
                "evidence": [{"source": "tea_leaves", "summary": "hmm", "weight": 0.5}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvalError, match="bad.json"):
        load_cases(tmp_path)


def test_an_empty_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(EvalError):
        load_cases(tmp_path)


# ---- the scoring -------------------------------------------------------------------


def test_accuracy_is_the_category_the_run_ended_with() -> None:
    outcomes = [
        _outcome(root_cause_category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION),
        _outcome(root_cause_category=ScenarioCategory.DB_DEADLOCK),
        _outcome(root_cause_category=None),
    ]

    assert score(outcomes, "scripted").root_cause_accuracy == pytest.approx(1 / 3)


def test_reaching_the_right_answer_and_losing_it_is_counted_separately() -> None:
    """The most useful row in the table: the model knew, and the critic threw it away."""
    outcomes = [
        _outcome(reached_correct=True, false_rejections=1, rejections=2, final_state="NEEDS_HUMAN")
    ]

    scores = score(outcomes, "scripted")

    assert scores.root_cause_accuracy == 0.0
    assert scores.reached_correct == 1.0
    assert scores.false_rejections == 1
    assert scores.completed == 0.0


def test_completed_means_completed_and_nothing_else() -> None:
    outcomes = [
        _outcome(final_state=str(State.COMPLETED)),
        _outcome(final_state=str(State.NEEDS_HUMAN)),
        _outcome(final_state=str(State.FAILED)),
    ]

    assert score(outcomes, "scripted").completed == pytest.approx(1 / 3)


def test_confidence_averages_only_the_runs_that_produced_one() -> None:
    outcomes = [_outcome(confidence=0.8), _outcome(confidence=None)]

    assert score(outcomes, "scripted").mean_confidence == pytest.approx(0.8)


def test_a_model_with_no_runs_scores_zero_rather_than_dividing_by_it() -> None:
    assert score([], "never ran").root_cause_accuracy == 0.0
