"""Settling on a cause, having it argued with, scoring it, and only then recommending anything.

The four states that turn a ranked shortlist into something a person can act on — and the two
places the run is designed to stop instead: a critic that rejects twice, and a score that does not
reach the threshold.
"""

from __future__ import annotations

import json

import pytest

from agents.categories import ScenarioCategory
from agents.confidence import CONFIDENCE_THRESHOLD
from agents.context import Hypothesis, RootCause
from agents.nodes.additional_evidence import CollectAdditionalEvidenceNode
from agents.nodes.critic import ValidateNode
from agents.nodes.hypotheses import GenerateHypothesesNode
from agents.nodes.rank import RankHypothesesNode
from agents.nodes.recommend import RecommendFixNode
from agents.nodes.root_cause import SelectRootCauseNode
from agents.state_machine import EventType, StateMachine
from agents.states import State
from tests.support import ScriptedProvider, context, pool_evidence

POOL_TITLE = "The orders connection pool was reduced from 200 to 20"


def _ranked(ctx, **overrides: object) -> Hypothesis:
    """A context with one hypothesis already ranked, which is what SELECT_ROOT_CAUSE expects."""
    fields: dict[str, object] = {
        "title": "Orders exhausted its connection pool",
        "description": "Every request now waits for a connection that never frees.",
        "category": ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION,
        "stated_confidence": 0.8,
        "score": 0.86,
        "rank": 1,
        "supporting_evidence": [0, 1],
    }
    hypothesis = Hypothesis(**{**fields, **overrides})  # type: ignore[arg-type]
    ctx.hypotheses = [hypothesis]

    return hypothesis


def _statement(**overrides: object) -> str:
    payload: dict[str, object] = {
        "title": POOL_TITLE,
        "category": "DB_CONNECTION_POOL_EXHAUSTION",
        "explanation": (
            "The pool cap was lowered [0], so requests queue for a connection and time out [1]."
        ),
        "evidence": [0, 1],
    }

    return json.dumps({**payload, **overrides})


def _verdict(**overrides: object) -> str:
    payload: dict[str, object] = {
        "supporting_evidence": [0, 1],
        "contradicting_evidence": [],
        "unsupported_claims": [],
        "concerns": [],
        "alternative": None,
        "valid": True,
        "confidence": 0.9,
    }

    return json.dumps({**payload, **overrides})


def _fix_plan(**overrides: object) -> str:
    payload: dict[str, object] = {
        "actions": [
            {
                "action_code": "restore pool size",
                "description": "Set the maximum pool size back to 200 and watch the headroom.",
                "tool_name": "update_env_and_restart",
            }
        ]
    }

    return json.dumps({**payload, **overrides})


def _pool_context():
    ctx = context()
    ctx.evidence = pool_evidence()

    return ctx


# ---- SELECT_ROOT_CAUSE -------------------------------------------------------------


async def test_the_top_hypothesis_becomes_the_root_cause() -> None:
    ctx = _pool_context()
    winner = _ranked(ctx)
    provider = ScriptedProvider([_statement()])

    transition = await SelectRootCauseNode(provider).run(ctx)

    assert transition.next_state is State.VALIDATE
    assert ctx.root_cause is not None
    assert ctx.root_cause.title == POOL_TITLE
    assert ctx.root_cause.category == ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION
    assert ctx.root_cause.hypothesis_title == winner.title
    assert winner.selected is True
    # No root cause event yet: it would have to carry a confidence nobody has computed.
    assert transition.events == ()


async def test_a_statement_that_cites_nothing_keeps_the_hypothesis_citations() -> None:
    """An empty citation list would drive evidence support to zero and sink a supported cause."""
    ctx = _pool_context()
    _ranked(ctx)
    provider = ScriptedProvider([_statement(evidence=[])])

    await SelectRootCauseNode(provider).run(ctx)

    assert ctx.root_cause is not None
    assert ctx.root_cause.supporting_evidence == [0, 1]
    assert any("cited no usable evidence" in note for note in ctx.notes)


async def test_a_statement_with_no_category_falls_back_to_the_hypothesis() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    provider = ScriptedProvider([_statement(category=None)])

    await SelectRootCauseNode(provider).run(ctx)

    assert ctx.root_cause is not None
    assert ctx.root_cause.category == ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION
    assert any("named no category" in note for note in ctx.notes)


async def test_the_alternatives_are_in_the_prompt_and_the_lone_case_says_so() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    ctx.hypotheses.append(Hypothesis(title="a bad deploy", score=0.3))
    provider = ScriptedProvider([_statement()])

    await SelectRootCauseNode(provider).run(ctx)

    assert "a bad deploy" in provider.last_prompt


async def test_no_hypothesis_to_write_up_stops_for_a_human() -> None:
    ctx = _pool_context()

    transition = await SelectRootCauseNode(ScriptedProvider([])).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN


# ---- VALIDATE ----------------------------------------------------------------------


async def test_an_accepted_conclusion_is_scored_and_goes_on_to_recommend() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(
        title=POOL_TITLE,
        explanation="the pool was lowered",
        category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION,
        supporting_evidence=[0, 1],
    )

    transition = await ValidateNode(ScriptedProvider([_verdict()])).run(ctx)

    assert transition.next_state is State.RECOMMEND_FIX
    assert ctx.root_cause.confidence >= CONFIDENCE_THRESHOLD
    assert ctx.root_cause.validator_confidence == pytest.approx(0.9)
    # One root cause event, carrying the score and the critic's own output.
    assert [event.type for event in transition.events] == [EventType.ROOT_CAUSE]
    payload = transition.events[0].payload
    assert payload is not None
    assert payload["confidence_breakdown"]["missing_terms"] == [
        "historical_similarity",
        "hypothesis_margin",
    ]
    assert payload["accepted_by_critic"] is True


async def test_the_critic_sees_every_fact_not_only_the_cited_ones() -> None:
    """The interesting failure is a conclusion that ignores the evidence standing against it."""
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(
        title=POOL_TITLE, explanation="the pool was lowered", supporting_evidence=[0]
    )
    provider = ScriptedProvider([_verdict()])

    await ValidateNode(provider).run(ctx)

    assert "deadlocks since reset" in provider.last_prompt


async def test_a_sound_conclusion_on_thin_evidence_stops_short_of_recommending() -> None:
    ctx = _pool_context()
    _ranked(ctx, supporting_evidence=[2])
    ctx.root_cause = RootCause(
        title="something to do with the database",
        explanation="there were no deadlocks, so it is something else",
        supporting_evidence=[2],
    )

    transition = await ValidateNode(ScriptedProvider([_verdict(confidence=0.5)])).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert transition.payload is not None
    assert transition.payload["below_threshold"] is True
    # The conclusion is still recorded: it is what the human is being asked to look at.
    assert [event.type for event in transition.events] == [EventType.ROOT_CAUSE]
    assert ctx.root_cause.confidence < CONFIDENCE_THRESHOLD
    assert ctx.recommendations == []


async def test_a_rejected_conclusion_goes_back_once_carrying_the_objections() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(
        title=POOL_TITLE, explanation="the pool was lowered", supporting_evidence=[0, 1]
    )
    verdict = _verdict(
        valid=False,
        confidence=0.3,
        concerns=["the connection count is for the whole estate, not for orders"],
        alternative="a downstream dependency is slow",
    )

    transition = await ValidateNode(ScriptedProvider([verdict])).run(ctx)

    assert transition.next_state is State.GENERATE_HYPOTHESES
    # The draft is discarded so the investigation ends with one conclusion, not a trail of them.
    assert ctx.root_cause is None
    assert transition.events == ()
    assert any("whole estate" in line for line in ctx.critic_feedback)
    assert any("a better explanation" in line for line in ctx.critic_feedback)


async def test_a_contradiction_leads_the_feedback_with_the_fact_that_caused_it() -> None:
    """"Evidence [3] says otherwise" is something the next round can act on.

    "The evidence is insufficient" is not, and produces the same hypothesis again in softer
    words — which is what the two-round loop looked like before the critic was asked to name the
    facts it was judging on.
    """
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(title=POOL_TITLE, explanation="deadlocks", supporting_evidence=[0])
    verdict = _verdict(
        valid=False,
        confidence=0.2,
        supporting_evidence=[],
        contradicting_evidence=[2],
        concerns=["the deadlock count is zero"],
    )

    await ValidateNode(ScriptedProvider([verdict])).run(ctx)

    line = ctx.critic_feedback[0]
    assert line.startswith(f'"{POOL_TITLE}" was rejected:'), (
        "the hypotheses prompt reads this list as the explanations not to propose again, so "
        "each line has to name one"
    )
    assert "evidence [2] contradicts this explanation" in line
    assert "the deadlock count is zero" in line


async def test_the_critic_reads_the_evidence_before_it_reaches_a_verdict() -> None:
    """The prompt asks for the two index lists first, and they are kept for a human to check."""
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(title=POOL_TITLE, explanation="the pool", supporting_evidence=[0])

    await ValidateNode(ScriptedProvider([_verdict(supporting_evidence=[0, 1])])).run(ctx)

    assert ctx.root_cause is not None
    assert ctx.root_cause.validator_output is not None
    assert ctx.root_cause.validator_output["supporting_evidence"] == [0, 1]
    assert ctx.root_cause.validator_output["contradicting_evidence"] == []


async def test_the_critic_is_on_v2_and_v1_is_still_reachable() -> None:
    """v1 is what the published 3B and 7B numbers were measured against, so it stays loadable."""
    provider = ScriptedProvider([])

    assert ValidateNode(provider).prompt_id == "critic.v2"
    assert ValidateNode(provider, prompt_version="v1").prompt_id == "critic.v1"


async def test_a_second_rejection_stops_and_keeps_what_it_had() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(
        title=POOL_TITLE, explanation="the pool was lowered", supporting_evidence=[0, 1]
    )
    ctx.counters["validate_failures"] = 1

    transition = await ValidateNode(
        ScriptedProvider([
            _verdict(
                valid=False,
                confidence=0.2,
                contradicting_evidence=[2],
                concerns=["still no"],
            )
        ])
    ).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert ctx.root_cause is not None
    assert [event.type for event in transition.events] == [EventType.ROOT_CAUSE]
    assert transition.events[0].payload is not None
    assert transition.events[0].payload["accepted_by_critic"] is False


async def test_a_veto_with_no_grounds_is_overruled_and_only_lowers_the_score() -> None:
    """The critic may reject for three reasons, and this verdict names none of them.

    It is the shape a 3B produces after prompt v2: supported by these facts, contradicted by
    nothing, no better explanation offered — and invalid anyway, because the evidence did not
    *prove* it. The objection survives as a concern and as a low validator confidence; what it
    no longer does is end the round.
    """
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(
        title=POOL_TITLE, explanation="the pool was lowered", supporting_evidence=[0, 1]
    )
    verdict = _verdict(
        valid=False,
        confidence=0.55,
        supporting_evidence=[0, 1],
        contradicting_evidence=[],
        alternative=None,
        concerns=["the evidence does not prove the timeouts came from the pool"],
    )

    transition = await ValidateNode(ScriptedProvider([verdict])).run(ctx)

    assert transition.next_state is not State.GENERATE_HYPOTHESES
    assert ctx.root_cause is not None, "the conclusion is kept rather than discarded"
    assert ctx.counters.get("validate_failures") is None, "it did not count as a rejection"
    assert transition.payload is not None
    assert transition.payload["verdict_overruled"] is True
    assert any("without naming a reason" in note for note in ctx.notes)
    # The critic still has a say: its 0.55 is a quarter of the score.
    assert ctx.root_cause.validator_confidence == 0.55
    assert ctx.root_cause.validator_output is not None
    assert ctx.root_cause.validator_output["valid"] is False, "what it said is recorded verbatim"


async def test_a_veto_that_names_a_contradiction_still_stands() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(title=POOL_TITLE, explanation="deadlocks", supporting_evidence=[0])
    verdict = _verdict(valid=False, confidence=0.3, contradicting_evidence=[2])

    transition = await ValidateNode(ScriptedProvider([verdict])).run(ctx)

    assert transition.next_state is State.GENERATE_HYPOTHESES
    assert ctx.root_cause is None


async def test_a_veto_that_rests_on_nothing_supporting_still_stands() -> None:
    ctx = _pool_context()
    _ranked(ctx)
    ctx.root_cause = RootCause(title=POOL_TITLE, explanation="a guess", supporting_evidence=[])
    verdict = _verdict(valid=False, confidence=0.1, supporting_evidence=[])

    transition = await ValidateNode(ScriptedProvider([verdict])).run(ctx)

    assert transition.next_state is State.GENERATE_HYPOTHESES


async def test_nothing_to_validate_stops_for_a_human() -> None:
    ctx = _pool_context()

    transition = await ValidateNode(ScriptedProvider([])).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN


# ---- RECOMMEND_FIX -----------------------------------------------------------------


async def test_recommendations_are_advisory_and_carry_no_arguments() -> None:
    ctx = _pool_context()
    ctx.root_cause = RootCause(
        title=POOL_TITLE, explanation="the pool was lowered", confidence=0.88
    )

    transition = await RecommendFixNode(ScriptedProvider([_fix_plan()])).run(ctx)

    assert transition.next_state is State.COMPLETED
    assert len(ctx.recommendations) == 1
    recommendation = ctx.recommendations[0]
    # Transcribed into an identifier rather than rejected for its shape.
    assert recommendation.action_code == "RESTORE_POOL_SIZE"
    assert recommendation.requires_approval is True
    assert recommendation.tool_name == "update_env_and_restart"
    assert recommendation.tool_args == {}
    assert [event.type for event in transition.events] == [EventType.RECOMMENDATION]


async def test_a_fix_plan_that_never_validates_keeps_the_diagnosis() -> None:
    ctx = _pool_context()
    ctx.root_cause = RootCause(title=POOL_TITLE, explanation="the pool was lowered")

    transition = await RecommendFixNode(ScriptedProvider(["no", "still no", "no"])).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert ctx.root_cause is not None


async def test_no_root_cause_means_no_recommendation() -> None:
    ctx = _pool_context()

    transition = await RecommendFixNode(ScriptedProvider([])).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN


# ---- the whole reasoning half, end to end ------------------------------------------


def _reasoning_machine(provider: ScriptedProvider, recorder) -> StateMachine:
    return StateMachine(
        {
            State.GENERATE_HYPOTHESES: GenerateHypothesesNode(provider),
            State.COLLECT_ADDITIONAL_EVIDENCE: CollectAdditionalEvidenceNode(),
            State.RANK_HYPOTHESES: RankHypothesesNode(),
            State.SELECT_ROOT_CAUSE: SelectRootCauseNode(provider),
            State.VALIDATE: ValidateNode(provider),
            State.RECOMMEND_FIX: RecommendFixNode(provider),
        },
        emit=recorder,
        start=State.GENERATE_HYPOTHESES,
    )


class _Recorder:
    def __init__(self) -> None:
        self.events = []

    async def __call__(self, event) -> None:
        self.events.append(event)


async def test_evidence_in_conclusion_out() -> None:
    """Four model calls from collected evidence to an approved-pending recommendation.

    The database collector is marked as already run, because otherwise the back-loop would send
    this machine to a state it has no node for — which is the runner refusing to continue past a
    plan it cannot execute, and is tested where the runner is.
    """
    ctx = _pool_context()
    ctx.visits[State.COLLECT_DATABASE] = 1
    provider = ScriptedProvider(
        [
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "title": "Orders exhausted its connection pool",
                            "description": "The pool cap was lowered.",
                            "category": "DB_CONNECTION_POOL_EXHAUSTION",
                            "confidence": 0.8,
                            "evidence": [0, 1],
                        },
                        {
                            "title": "A deadlock is blocking writes",
                            "description": "Sessions waiting on each other would look like this.",
                            "category": "DB_DEADLOCK",
                            "confidence": 0.3,
                            "evidence": [2],
                        },
                    ]
                }
            ),
            _statement(),
            _verdict(),
            _fix_plan(),
        ]
    )
    recorder = _Recorder()

    result = await _reasoning_machine(provider, recorder).run(ctx)

    assert result.final_state is State.COMPLETED
    assert result.succeeded is True
    assert ctx.root_cause is not None
    assert ctx.root_cause.title == POOL_TITLE
    assert ctx.root_cause.confidence >= CONFIDENCE_THRESHOLD
    # Both hypotheses were ranked, so the margin term was measurable this time.
    assert ctx.root_cause.confidence_breakdown["missing_terms"] == ["historical_similarity"]
    assert [r.action_code for r in ctx.recommendations] == ["RESTORE_POOL_SIZE"]
    assert ctx.llm_calls == 4

    kinds = [event.type for event in recorder.events]
    assert kinds.count(EventType.HYPOTHESIS) == 2
    assert kinds.count(EventType.ROOT_CAUSE) == 1
    assert kinds.count(EventType.RECOMMENDATION) == 1
    assert kinds[-1] is EventType.COMPLETED
