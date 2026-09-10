"""Forming candidate explanations, deciding whether to go back for more, and ranking them.

The three states that turn evidence into a shortlist. Every model answer here is scripted: what
is under test is what the nodes do with an answer, not what a 3B model would have said.
"""

from __future__ import annotations

import json

import pytest

from agents.categories import ScenarioCategory
from agents.context import Hypothesis
from agents.nodes.additional_evidence import (
    MIN_BUDGET_FOR_COLLECTOR,
    CollectAdditionalEvidenceNode,
)
from agents.nodes.hypotheses import GenerateHypothesesNode
from agents.nodes.rank import SCORE_EVIDENCE_SHARE, SCORE_MODEL_SHARE, RankHypothesesNode
from agents.state_machine import EventType
from agents.states import State
from tests.support import ScriptedProvider, context, pool_evidence


def _answer(*hypotheses: dict[str, object]) -> str:
    return json.dumps({"hypotheses": list(hypotheses)})


def _pool_hypothesis(**overrides: object) -> dict[str, object]:
    proposed: dict[str, object] = {
        "title": "Orders exhausted its connection pool",
        "description": "The pool was reduced to 20 and every request now waits for a connection.",
        "category": "DB_CONNECTION_POOL_EXHAUSTION",
        "confidence": 0.8,
        "evidence": [0, 1],
    }

    return {**proposed, **overrides}


# ---- GENERATE_HYPOTHESES -----------------------------------------------------------


async def test_hypotheses_are_recorded_with_what_they_cite() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    provider = ScriptedProvider(
        [
            _answer(
                _pool_hypothesis(),
                {
                    "title": "A slow query is holding connections",
                    "description": "A missing index would hold each connection longer.",
                    "category": "DB_SLOW_QUERY_MISSING_INDEX",
                    "confidence": 0.4,
                    "evidence": [1],
                },
            )
        ]
    )

    transition = await GenerateHypothesesNode(provider).run(ctx)

    assert transition.next_state is State.COLLECT_ADDITIONAL_EVIDENCE
    assert [h.title for h in ctx.hypotheses] == [
        "Orders exhausted its connection pool",
        "A slow query is holding connections",
    ]
    assert ctx.hypotheses[0].category == ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION
    assert ctx.hypotheses[0].stated_confidence == pytest.approx(0.8)
    assert ctx.hypotheses[0].supporting_evidence == [0, 1]
    # Ranking has not run: a hypothesis arrives here without a score.
    assert ctx.hypotheses[0].score == 0.0


async def test_the_model_call_is_counted_against_the_investigation() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()

    await GenerateHypothesesNode(ScriptedProvider([_answer(_pool_hypothesis())])).run(ctx)

    assert ctx.llm_calls == 1
    assert ctx.prompt_tokens == 100
    assert ctx.completion_tokens == 20


async def test_a_citation_that_points_nowhere_is_dropped_and_said_so() -> None:
    """A cited index outside the evidence would otherwise inflate the support of nothing."""
    ctx = context()
    ctx.evidence = pool_evidence()
    provider = ScriptedProvider([_answer(_pool_hypothesis(evidence=[0, 9, 1, 0]))])

    transition = await GenerateHypothesesNode(provider).run(ctx)

    assert ctx.hypotheses[0].supporting_evidence == [0, 1]
    assert transition.payload is not None
    assert transition.payload["citations_dropped"] == 1
    assert any("outside the collected evidence" in note for note in ctx.notes)


async def test_a_category_that_is_not_a_scenario_becomes_none() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    provider = ScriptedProvider([_answer(_pool_hypothesis(category="DB_POOL_PROBLEM"))])

    await GenerateHypothesesNode(provider).run(ctx)

    assert ctx.hypotheses[0].category is None


async def test_with_no_evidence_the_model_is_not_asked_at_all() -> None:
    """Explaining an incident nothing was collected about is the failure mode, not a fallback."""
    ctx = context()
    provider = ScriptedProvider([])

    transition = await GenerateHypothesesNode(provider).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert provider.calls == 0
    assert ctx.hypotheses == []


async def test_output_that_never_validates_stops_for_a_human_and_still_costs() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    provider = ScriptedProvider(["not json", "still not json", "{"])

    transition = await GenerateHypothesesNode(provider).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert provider.calls == 3
    # The three round trips happened, so the run is charged for them.
    assert ctx.llm_calls == 3


async def test_the_second_round_is_shown_what_the_critic_rejected() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.critic_feedback = ["the connection count is from a different service"]
    provider = ScriptedProvider([_answer(_pool_hypothesis())])

    await GenerateHypothesesNode(provider).run(ctx)

    assert "different service" in provider.last_prompt
    # And the evidence reaches the prompt as numbered lines it can cite.
    assert "[0] (database, weight 0.90)" in provider.last_prompt


async def test_regenerating_replaces_the_previous_hypotheses() -> None:
    """A hypothesis the critic rejected must not survive to win the next ranking."""
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [Hypothesis(title="the rejected one", score=0.9)]
    provider = ScriptedProvider([_answer(_pool_hypothesis())])

    transition = await GenerateHypothesesNode(provider).run(ctx)

    assert [h.title for h in ctx.hypotheses] == ["Orders exhausted its connection pool"]
    assert transition.payload is not None
    assert transition.payload["round"] == 1


# ---- COLLECT_ADDITIONAL_EVIDENCE ---------------------------------------------------


async def test_a_hypothesis_whose_signal_was_never_collected_sends_the_machine_back() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [
        Hypothesis(
            title="Orders exhausted its connection pool",
            category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION,
        )
    ]

    transition = await CollectAdditionalEvidenceNode().run(ctx)

    assert transition.next_state is State.COLLECT_DATABASE
    # Requeued rather than merely transitioned to, so the collector's own advance() finds it.
    assert ctx.plan == [State.COLLECT_DATABASE]
    assert ctx.iteration == 1


async def test_a_collector_that_already_ran_is_not_asked_again() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.visits[State.COLLECT_DATABASE] = 1
    ctx.hypotheses = [
        Hypothesis(
            title="Orders exhausted its connection pool",
            category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION,
        )
    ]

    transition = await CollectAdditionalEvidenceNode().run(ctx)

    assert transition.next_state is State.RANK_HYPOTHESES
    assert ctx.iteration == 0
    assert ctx.plan == []


async def test_an_uncategorised_hypothesis_is_not_guessed_a_collector_for() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [Hypothesis(title="something is wrong with orders")]

    transition = await CollectAdditionalEvidenceNode().run(ctx)

    assert transition.next_state is State.RANK_HYPOTHESES


async def test_the_iteration_limit_stops_the_loop_and_says_why() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.iteration = ctx.max_iterations
    ctx.hypotheses = [
        Hypothesis(title="pool", category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION)
    ]

    transition = await CollectAdditionalEvidenceNode().run(ctx)

    assert transition.next_state is State.RANK_HYPOTHESES
    assert transition.payload is not None
    assert transition.payload["reason"] == "iterations"
    assert transition.payload["wanted"] == State.COLLECT_DATABASE.value


async def test_too_little_budget_left_is_not_spent_on_a_collector_that_would_raise() -> None:
    """Arriving at a collector with no budget ends the run in NEEDS_HUMAN one step later."""
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.tool_calls_made = ctx.tool_budget - (MIN_BUDGET_FOR_COLLECTOR - 1)
    ctx.hypotheses = [
        Hypothesis(title="pool", category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION)
    ]

    transition = await CollectAdditionalEvidenceNode().run(ctx)

    assert transition.next_state is State.RANK_HYPOTHESES
    assert transition.payload is not None
    assert transition.payload["reason"] == "budget"
    # The iteration was spent deciding to go back, and giving up does not refund it.
    assert ctx.iteration == 1


async def test_the_most_plausible_gap_is_the_one_taken() -> None:
    """One collector per visit, and it is the leading hypothesis that gets it."""
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [
        Hypothesis(title="a bad deploy", category=ScenarioCategory.BAD_DEPLOYMENT_REGRESSION),
        Hypothesis(title="pool", category=ScenarioCategory.DB_CONNECTION_POOL_EXHAUSTION),
    ]

    transition = await CollectAdditionalEvidenceNode().run(ctx)

    assert transition.next_state is State.CHECK_DEPLOYMENTS
    assert ctx.plan == [State.CHECK_DEPLOYMENTS]


# ---- RANK_HYPOTHESES ---------------------------------------------------------------


async def test_ranking_is_evidence_first_and_the_model_second() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [
        Hypothesis(title="confident and unsupported", stated_confidence=1.0),
        Hypothesis(title="supported", stated_confidence=0.2, supporting_evidence=[0, 1, 2]),
    ]

    transition = await RankHypothesesNode().run(ctx)

    assert [h.title for h in ctx.hypotheses] == ["supported", "confident and unsupported"]
    assert ctx.hypotheses[0].rank == 1
    assert ctx.hypotheses[0].score == pytest.approx(
        SCORE_EVIDENCE_SHARE * 1.0 + SCORE_MODEL_SHARE * 0.2
    )
    assert ctx.hypotheses[1].score == pytest.approx(SCORE_MODEL_SHARE * 1.0)
    assert transition.next_state is State.SELECT_ROOT_CAUSE


async def test_every_ranked_hypothesis_is_announced_once_with_its_rank() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [
        Hypothesis(title="first", stated_confidence=0.9, supporting_evidence=[0]),
        Hypothesis(title="second", stated_confidence=0.1),
    ]

    transition = await RankHypothesesNode().run(ctx)

    assert [event.type for event in transition.events] == [EventType.HYPOTHESIS] * 2
    payloads = [event.payload for event in transition.events]
    assert payloads[0] is not None and payloads[0]["rank"] == 1
    assert transition.payload is not None
    assert transition.payload["margin"] is not None


async def test_equal_scores_keep_the_order_the_model_proposed() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [
        Hypothesis(title="proposed first", stated_confidence=0.5, supporting_evidence=[0]),
        Hypothesis(title="proposed second", stated_confidence=0.5, supporting_evidence=[0]),
    ]

    await RankHypothesesNode().run(ctx)

    assert [h.title for h in ctx.hypotheses] == ["proposed first", "proposed second"]


async def test_a_lone_hypothesis_ranks_and_says_it_had_no_competitor() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.hypotheses = [Hypothesis(title="the only one", stated_confidence=0.9)]

    transition = await RankHypothesesNode().run(ctx)

    assert transition.payload is not None
    assert transition.payload["margin"] is None
    assert "no runner-up" in transition.message


async def test_nothing_to_rank_stops_for_a_human() -> None:
    ctx = context()

    transition = await RankHypothesesNode().run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
