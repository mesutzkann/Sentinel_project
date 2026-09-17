"""What the agent's own model calls leave behind in ``model_predictions``.

Until now only the ``/llm`` endpoint reported its calls, so the table described the one caller
nothing in production uses: an investigation made four to six model calls and the dashboard's
cost-per-purpose panel never heard about any of them. These tests are about the row, not about
the reasoning — what purpose it is filed under, what a repair loop does to it, and that a call
which never validated is still a row.
"""

from __future__ import annotations

import json

from agents.context import RootCause
from agents.nodes.critic import ValidateNode
from agents.nodes.hypotheses import GenerateHypothesesNode
from agents.postmortem import PostmortemWriter
from agents.states import State
from reporting.predictions import OUTPUT_CHARS, ModelPurpose
from tests.support import RecordingReporter, ScriptedProvider, context, pool_evidence

HYPOTHESES = json.dumps(
    {
        "hypotheses": [
            {
                "title": "Orders exhausted its connection pool",
                "description": "The pool was cut to 20 and every request waits for a connection.",
                "category": "DB_CONNECTION_POOL_EXHAUSTION",
                "confidence": 0.8,
                "evidence": [0, 1],
            }
        ]
    }
)

VERDICT = json.dumps(
    {
        "supporting_evidence": [0, 1],
        "contradicting_evidence": [],
        "unsupported_claims": [],
        "concerns": [],
        "alternative": None,
        "valid": True,
        "confidence": 0.9,
    }
)

DRAFT = json.dumps(
    {
        "summary": "Orders timed out after the connection pool was cut from 200 to 20.",
        "what_we_saw": "The pool reported 200 of 200 in use and the logs carried 347 timeouts.",
        "lessons": ["A pool size is a capacity limit and belongs under review."],
        "prevention": ["Alert when pool wait time exceeds 100 ms."],
    }
)


def concluded():
    """A run that reached a conclusion, which is what VALIDATE and the postmortem both need."""
    ctx = context()
    ctx.evidence = pool_evidence()
    ctx.root_cause = RootCause(
        title="Connection pool exhausted on orders",
        explanation="The pool was cut from 200 to 20 [0], so requests queued and timed out [1].",
        category="DB_CONNECTION_POOL_EXHAUSTION",
        supporting_evidence=[0, 1],
    )

    return ctx


async def test_a_reasoning_call_becomes_one_row_against_the_investigation() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    reporter = RecordingReporter()

    await GenerateHypothesesNode(ScriptedProvider([HYPOTHESES]), reporter=reporter).run(ctx)

    assert len(reporter.records) == 1
    record = reporter.records[0]
    assert record.purpose is ModelPurpose.REASONING
    assert record.investigation_id == ctx.investigation_id
    assert record.model_name == "scripted"
    assert record.valid_json
    # The parsed object, not the model's raw text: the failures are the rows where the exact
    # bytes matter, and there the raw text is what is stored.
    assert "Orders exhausted its connection pool" in (record.output or "")
    assert (record.prompt_tokens, record.completion_tokens, record.latency_ms) == (100, 20, 5)


async def test_a_repair_loop_is_one_row_carrying_what_the_caller_waited() -> None:
    """Two round trips, one row. Splitting them would make a model that retries look cheap."""
    ctx = context()
    ctx.evidence = pool_evidence()
    reporter = RecordingReporter()
    provider = ScriptedProvider(["not json at all", HYPOTHESES])

    await GenerateHypothesesNode(provider, reporter=reporter).run(ctx)

    assert provider.calls == 2
    assert len(reporter.records) == 1
    record = reporter.records[0]
    assert record.valid_json
    assert (record.prompt_tokens, record.completion_tokens, record.latency_ms) == (200, 40, 10)
    # And the context's own tally still counts the round trips, which is the other report.
    assert ctx.llm_calls == 2


async def test_a_call_that_never_validated_is_still_a_row() -> None:
    ctx = context()
    ctx.evidence = pool_evidence()
    reporter = RecordingReporter()
    provider = ScriptedProvider(["nope", "still nope", "nope again"])

    transition = await GenerateHypothesesNode(provider, reporter=reporter).run(ctx)

    assert transition.next_state is State.NEEDS_HUMAN
    assert len(reporter.records) == 1
    record = reporter.records[0]
    assert not record.valid_json
    # The last attempt verbatim. A success-only table would put the structured-output rate at
    # 1.0 by construction, and the text is what says why this one failed.
    assert record.output == "nope again"
    assert record.prompt_tokens == 300


async def test_the_critic_is_filed_under_validation() -> None:
    reporter = RecordingReporter()

    await ValidateNode(ScriptedProvider([VERDICT]), reporter=reporter).run(concluded())

    assert [r.purpose for r in reporter.records] == [ModelPurpose.VALIDATION]


async def test_the_postmortem_is_filed_under_its_own_purpose() -> None:
    """Written after the run has ended, so its cost is not part of thinking the run through."""
    ctx = concluded()
    reporter = RecordingReporter()

    postmortem = await PostmortemWriter(ScriptedProvider([DRAFT]), reporter=reporter).write(ctx)

    assert postmortem is not None
    assert [r.purpose for r in reporter.records] == [ModelPurpose.POSTMORTEM]
    assert reporter.records[0].investigation_id == ctx.investigation_id


async def test_a_rambling_failure_is_cut_but_a_long_answer_is_not() -> None:
    """The column is `jsonb`. Truncating an object that validated would leave it unparseable."""
    filler = "x" * (OUTPUT_CHARS * 2)
    ctx = context()
    ctx.evidence = pool_evidence()
    reporter = RecordingReporter()

    await GenerateHypothesesNode(
        ScriptedProvider([filler] * 3), reporter=reporter
    ).run(ctx)
    long_answer = json.dumps(
        {
            "hypotheses": [
                {
                    "title": "Orders exhausted its connection pool",
                    "description": "y" * (OUTPUT_CHARS * 2),
                    "category": "DB_CONNECTION_POOL_EXHAUSTION",
                    "confidence": 0.8,
                    "evidence": [0, 1],
                }
            ]
        }
    )
    second = context()
    second.evidence = pool_evidence()
    await GenerateHypothesesNode(
        ScriptedProvider([long_answer]), reporter=reporter
    ).run(second)

    failed, succeeded = reporter.records
    assert len(failed.output or "") == OUTPUT_CHARS
    # Whole, and still parseable: what the dashboard shows is read out of a `jsonb` column.
    assert json.loads(succeeded.output or "")["hypotheses"][0]["description"].startswith("yyy")


async def test_a_node_without_a_reporter_runs_the_same() -> None:
    """The benchmarks and every other test build nodes without one; nothing may depend on it."""
    ctx = context()
    ctx.evidence = pool_evidence()

    transition = await GenerateHypothesesNode(ScriptedProvider([HYPOTHESES])).run(ctx)

    assert transition.next_state is State.COLLECT_ADDITIONAL_EVIDENCE
    assert len(ctx.hypotheses) == 1
