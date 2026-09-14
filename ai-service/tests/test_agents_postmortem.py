"""The postmortem: what the model is allowed to write, and what it is not.

The property this file exists for is the division of labour. A postmortem is the document the
*next* investigation retrieves as precedent, so an invented timeline or a confidence the model
chose would not be a bad paragraph — it would be a false record indexed next to ten true ones,
with nothing to tell them apart. Everything factual here is rendered from the run.
"""

from __future__ import annotations

import pytest

from agents.context import EvidenceItem, EvidenceSource, Recommendation, RootCause
from agents.postmortem import (
    GENERATED_DIR,
    PostmortemDraft,
    PostmortemWriter,
    build,
)
from rag.documents import SourceType
from rag.ingest import parse_document
from tests.support import ScriptedProvider, context

DRAFT = PostmortemDraft(
    summary=(
        "Orders timed out for twenty minutes after a configuration change cut the connection "
        "pool from 200 to 20."
    ),
    what_we_saw=(
        "Latency reached 4.2 s at p95 while the error rate climbed to 18%, and the logs carried "
        "repeated `Timeout waiting for connection from pool`."
    ),
    lessons=["A pool size is a capacity limit and belongs under review like any other."],
    prevention=["Alert when pool wait time exceeds 100 ms."],
)


def investigated():
    """A run that reached a conclusion, which is the only kind that produces a postmortem."""
    ctx = context()
    ctx.root_cause = RootCause(
        title="Connection pool exhausted on orders",
        explanation="The pool was cut from 200 to 20 [0], so requests queued and timed out [1].",
        category="DB_CONNECTION_POOL_EXHAUSTION",
        confidence=0.78,
        validator_output={"verdict": "valid"},
    )
    ctx.evidence = [
        EvidenceItem(
            source=EvidenceSource.SOURCE_CODE,
            summary="MaxPoolSize is 20 in appsettings.json",
            weight=0.8,
            tool="source-code-mcp/read_file",
        ),
        EvidenceItem(
            source=EvidenceSource.METRICS,
            summary="p95 latency 4.2s, error rate 18%",
            weight=0.8,
            tool="metrics-mcp/get_response_time",
        ),
    ]
    ctx.recommendations = [
        Recommendation(action_code="UPDATE_CONFIG", description="Restore MaxPoolSize to 200")
    ]

    return ctx


EVENTS = [
    {
        "type": "step_completed",
        "state": "PLAN",
        "message": "FULL_INVESTIGATION on orders: 4 collector(s)",
        "timestamp": "2026-09-14T09:12:00+00:00",
    },
    {
        "type": "evidence_found",
        "state": "COLLECT_METRICS",
        "message": "p95 latency 4.2s",
        "timestamp": "2026-09-14T09:13:00+00:00",
    },
    {
        "type": "step_completed",
        "state": "SELECT_ROOT_CAUSE",
        "message": "Connection pool exhausted on orders",
        "timestamp": "2026-09-14T09:15:00+00:00",
    },
]


def test_the_facts_are_rendered_from_the_run_not_from_the_model() -> None:
    """The conclusion, its confidence, the evidence and the actions are all the run's."""
    postmortem = build(investigated(), DRAFT, duration_ms=245_000, events=EVENTS)

    assert "Connection pool exhausted on orders" in postmortem.markdown
    assert "confidence: 0.78" in postmortem.markdown
    assert "MaxPoolSize is 20 in appsettings.json" in postmortem.markdown
    assert "**UPDATE_CONFIG**" in postmortem.markdown
    assert "The critic's verdict on this conclusion: valid." in postmortem.markdown


def test_the_timeline_comes_from_the_emitted_events() -> None:
    """The section a model would most willingly invent, and a reader most readily trusts."""
    postmortem = build(investigated(), DRAFT, events=EVENTS)

    assert "| 09:12 | PLAN | FULL_INVESTIGATION on orders: 4 collector(s) |" in postmortem.markdown
    assert "| 09:15 | SELECT_ROOT_CAUSE |" in postmortem.markdown

    # Only the transitions. Every evidence_found event in the timeline would make it a second
    # copy of the evidence list, which the document already has with its indices.
    assert "COLLECT_METRICS" not in postmortem.markdown.split("## Root cause")[0].split(
        "## Timeline"
    )[-1]


def test_a_run_with_no_timeline_omits_the_section_rather_than_inventing_one() -> None:
    postmortem = build(investigated(), DRAFT, events=None)

    assert "## Timeline" not in postmortem.markdown
    assert "## Root cause" in postmortem.markdown


def test_evidence_keeps_the_indices_the_run_cited() -> None:
    """A hypothesis cited [0]; a document that renumbered would break the only audit trail."""
    postmortem = build(investigated(), DRAFT)

    assert "`[0]` (source_code, weight 0.80) via `source-code-mcp/read_file`" in postmortem.markdown
    assert "`[1]` (metrics, weight 0.80)" in postmortem.markdown


def test_the_document_is_what_the_corpus_loader_would_read_back() -> None:
    """The file is written to the corpus, so the next ingest parses it. Byte for byte.

    If the body the store indexes differed from the body the loader returns for the same file —
    by a stripped blank line, or an int where the loader reads text — the document would carry
    two content hashes and be re-embedded on every corpus walk forever.
    """
    postmortem = build(investigated(), DRAFT, duration_ms=245_000)
    reloaded = parse_document(postmortem.markdown, path=postmortem.filename)

    assert reloaded.content_hash == postmortem.document.content_hash
    assert reloaded.metadata == postmortem.document.metadata
    assert reloaded.source_type is SourceType.POSTMORTEM
    assert reloaded.external_id == "INC-00142"
    assert reloaded.service == "orders"


def test_it_is_filed_where_a_generated_document_can_be_told_apart() -> None:
    postmortem = build(investigated(), DRAFT)

    assert postmortem.filename.startswith(f"{GENERATED_DIR}/INC-00142-")
    assert postmortem.filename.endswith(".md")
    assert "source: generated" in postmortem.markdown
    assert "Not reviewed by a human." in postmortem.markdown


@pytest.mark.asyncio
async def test_a_run_that_reached_no_conclusion_writes_nothing() -> None:
    """NEEDS_HUMAN has, by construction, a conclusion nobody stood behind."""
    ctx = context()
    provider = ScriptedProvider([])

    assert await PostmortemWriter(provider).write(ctx) is None
    assert provider.calls == 0, "the model must not be asked to write up a run with no answer"


@pytest.mark.asyncio
async def test_a_model_that_will_not_produce_the_object_costs_the_document_not_the_run() -> None:
    writer = PostmortemWriter(ScriptedProvider(["not json"] * 3), max_attempts=3)

    assert await writer.write(investigated()) is None


@pytest.mark.asyncio
async def test_the_prompt_carries_the_conclusion_and_the_evidence() -> None:
    """The model is writing up an investigation, not redoing it."""
    provider = ScriptedProvider([DRAFT.model_dump_json()])
    postmortem = await PostmortemWriter(provider).write(investigated(), duration_ms=245_000)

    assert postmortem is not None
    assert postmortem.usage["prompt_tokens"] > 0

    prompt = provider.last_prompt
    assert "Connection pool exhausted on orders" in prompt
    assert "MaxPoolSize is 20 in appsettings.json" in prompt
    assert "UPDATE_CONFIG" in prompt
    assert "not reopening it" in prompt.casefold()


@pytest.mark.asyncio
async def test_the_structured_form_carries_what_the_backend_stores() -> None:
    postmortem = await PostmortemWriter(ScriptedProvider([DRAFT.model_dump_json()])).write(
        investigated()
    )

    assert postmortem is not None
    assert postmortem.structured["category"] == "DB_CONNECTION_POOL_EXHAUSTION"
    assert postmortem.structured["confidence"] == 0.78
    assert postmortem.structured["evidence_count"] == 2
    assert postmortem.structured["recommendations"] == ["UPDATE_CONFIG"]
    assert postmortem.structured["lessons"] == DRAFT.lessons
