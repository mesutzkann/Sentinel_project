"""The agent's contract with the model, checked as a grammar rather than only as a validator.

These classes are handed to constrained decoding, so their JSON schema is what the model is
*allowed* to generate. That makes one property worth a test of its own: a field with a default is
absent from `required`, the grammar then permits the model to omit the key, and a model that
follows the grammar strictly will. The benchmark caught it as a 7B that could not categorise
anything, when it was a 7B that had been told the field was optional.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from agents.categories import ScenarioCategory
from agents.schemas import (
    CriticVerdict,
    FixPlan,
    HypothesisSet,
    ProposedAction,
    ProposedHypothesis,
    RootCauseStatement,
)

SCHEMAS: tuple[type[BaseModel], ...] = (
    ProposedHypothesis,
    HypothesisSet,
    RootCauseStatement,
    CriticVerdict,
    ProposedAction,
    FixPlan,
)


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.__name__)
def test_no_field_is_optional(schema: type[BaseModel]) -> None:
    """An optional field is one constrained decoding lets the model skip. Nullable is not the same.

    `category` may be null and must be present. That distinction is the whole fix: "none of these
    fits" is an answer the prompt asks for, and it has to be said rather than left out.
    """
    emitted = schema.model_json_schema()
    optional = set(emitted.get("properties", {})) - set(emitted.get("required", []))

    assert not optional, f"{schema.__name__} lets the model omit {sorted(optional)}"


def test_the_nullable_fields_are_still_nullable() -> None:
    hypothesis = ProposedHypothesis(
        title="something",
        description="a mechanism",
        category=None,
        confidence=0.5,
        evidence=[],
    )

    assert hypothesis.category is None
    assert hypothesis.evidence == []


def test_a_missing_field_is_a_validation_error_the_repair_turn_can_name() -> None:
    """Without constrained decoding, a skipped key costs one round trip. It used to cost
    silence."""
    with pytest.raises(ValidationError, match="evidence"):
        ProposedHypothesis(title="t", description="d", category=None, confidence=0.5)  # type: ignore[call-arg]


def test_a_category_that_is_not_a_scenario_is_dropped_rather_than_rejected() -> None:
    """The one place a wrong value is forgiven, because a near miss is a miss and not a retry."""
    hypothesis = ProposedHypothesis(
        title="t",
        description="d",
        category="DB_POOL_EXHAUSTED",
        confidence=0.5,
        evidence=[0],
    )

    assert hypothesis.category is None


def test_a_scenario_code_survives_transcription() -> None:
    statement = RootCauseStatement(
        title="t",
        category="db-deadlock",
        explanation="e",
        evidence=[1],
    )

    assert statement.category == ScenarioCategory.DB_DEADLOCK


def test_an_action_code_is_transcribed_into_an_identifier() -> None:
    action = ProposedAction(
        action_code="restore the pool size!",
        description="d",
        tool_name=None,
    )

    assert action.action_code == "RESTORE_THE_POOL_SIZE"


def test_a_hypothesis_set_needs_at_least_one_and_at_most_five() -> None:
    def hypothesis(n: int) -> dict[str, object]:
        return {
            "title": f"h{n}",
            "description": "d",
            "category": None,
            "confidence": 0.5,
            "evidence": [],
        }

    with pytest.raises(ValidationError):
        HypothesisSet(hypotheses=[])  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        HypothesisSet.model_validate({"hypotheses": [hypothesis(n) for n in range(6)]})

    assert len(HypothesisSet.model_validate({"hypotheses": [hypothesis(1)]}).hypotheses) == 1


def test_a_fix_plan_caps_the_actions() -> None:
    action = {"action_code": "DO_IT", "description": "d", "tool_name": None}

    with pytest.raises(ValidationError):
        FixPlan.model_validate({"actions": [action] * 4})

    assert len(FixPlan.model_validate({"actions": [action]}).actions) == 1


def test_the_critic_must_state_a_confidence_it_cannot_leave_to_a_default() -> None:
    """A silent 0.5 here is a quarter of the confidence score invented by a missing key."""
    with pytest.raises(ValidationError, match="confidence"):
        CriticVerdict.model_validate(
            {"valid": False, "concerns": [], "unsupported_claims": [], "alternative": None}
        )
