"""What the retrieved chunks look like by the time a model sees them.

The budget is the reason this file exists. Everything else here is formatting, and formatting is
tested because a citation the agent cannot resolve back to a chunk id makes an investigation's
evidence unverifiable.
"""

from __future__ import annotations

from rag.chunking import estimate_tokens
from rag.context_builder import DEFAULT_TOKEN_BUDGET, ContextBuilder
from rag.documents import RetrievedChunk, SourceType, StoredChunk


def _hit(
    chunk_id: str,
    content: str,
    rank: int = 1,
    *,
    title: str = "Runbook — connection pool exhaustion",
    source_type: SourceType = SourceType.RUNBOOK,
    service: str | None = "orders",
    external_id: str | None = "RB-001",
    section: str | None = "Runbook > Fix",
) -> RetrievedChunk:
    stored = StoredChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        content=content,
        chunk_index=0,
        title=title,
        source_type=source_type,
        service=service,
        external_id=external_id,
        path="runbooks/connection-pool-exhaustion.md",
        section=section,
    )

    return RetrievedChunk(chunk=stored, score=0.9, rank=rank, retriever="cross_encoder")


# ---------------------------------------------------------------------- labels ----


def test_every_chunk_is_numbered_and_labelled() -> None:
    """The label is what makes a citation resolvable.

    Without it, a root cause built on a runbook is indistinguishable from one the model
    invented, which is the difference between evidence and an assertion.
    """
    context = ContextBuilder().build([_hit("c1", "Restore MaxPoolSize to 200.")])

    assert "[S1] runbook · orders · RB-001 · Runbook > Fix" in context.text
    assert "Restore MaxPoolSize to 200." in context.text


def test_the_sources_come_back_as_data_as_well_as_text() -> None:
    # The prompt needs the label inline; the investigation record needs the chunk id behind S1.
    context = ContextBuilder().build([_hit("c1", "Restore MaxPoolSize to 200.")])

    assert [(s.ref, s.chunk_id) for s in context.sources] == [("S1", "c1")]


def test_a_chunk_with_no_service_or_id_still_gets_a_label() -> None:
    context = ContextBuilder().build(
        [
            _hit(
                "c1",
                "The estate is five services behind a gateway.",
                title="Sample estate architecture",
                source_type=SourceType.ARCHITECTURE,
                service=None,
                external_id=None,
                section=None,
            )
        ]
    )

    assert "[S1] architecture · Sample estate architecture" in context.text


def test_refs_are_numbered_in_rank_order() -> None:
    context = ContextBuilder().build(
        [_hit("c1", "first", rank=1), _hit("c2", "second", rank=2), _hit("c3", "third", rank=3)]
    )

    assert [s.ref for s in context.sources] == ["S1", "S2", "S3"]
    assert context.text.index("[S1]") < context.text.index("[S2]") < context.text.index("[S3]")


# ---------------------------------------------------------------------- budget ----


def test_the_budget_is_enforced() -> None:
    """A retrieval that quietly returns 6000 tokens does not fail.

    It pushes the incident out of the model's window, and the model answers a question it can no
    longer see. That failure is invisible from the response, so it is prevented here.
    """
    long_chunks = [_hit(f"c{i}", f"unique-{i} " + "word " * 400, rank=i) for i in range(1, 11)]

    context = ContextBuilder(token_budget=600).build(long_chunks)

    assert context.token_count <= 600
    assert context.dropped


def test_a_chunk_that_does_not_fit_does_not_stop_the_ones_after_it() -> None:
    # Chunks run from 80 tokens to 400. Abandoning the list at the first long one would drop a
    # relevant short note to save a place nothing else could use.
    chunks = [
        _hit("small-1", "alpha note", rank=1),
        _hit("huge", "beta " * 500, rank=2),
        _hit("small-2", "gamma note", rank=3),
    ]

    context = ContextBuilder(token_budget=120).build(chunks)

    assert [s.chunk_id for s in context.sources] == ["small-1", "small-2"]
    assert context.dropped == ["huge"]


def test_the_default_budget_is_the_one_planning_settled_on() -> None:
    assert DEFAULT_TOKEN_BUDGET == 3000
    assert ContextBuilder().build([_hit("c1", "short")]).token_count < DEFAULT_TOKEN_BUDGET


def test_the_reported_size_is_the_size_of_the_finished_text() -> None:
    # Accumulating the parts and reporting that as the whole is how an off-by-one in the joins
    # becomes invisible.
    context = ContextBuilder().build([_hit("c1", "alpha"), _hit("c2", "beta", rank=2)])

    assert context.token_count == estimate_tokens(context.text)


# -------------------------------------------------------------------- overlap ----


def test_the_chunkers_overlap_is_not_paid_for_twice() -> None:
    """Adjacent chunks of one document deliberately repeat whole blocks.

    That is right for retrieval and wasteful in a prompt: the shared paragraph is dropped from
    the second chunk, and the part that is only in the second chunk survives.
    """
    shared = "Connection acquisition is not the database being slow."
    first = _hit("c1", f"{shared}\n\nRaise MaxPoolSize back to 200.", rank=1)
    second = _hit("c2", f"{shared}\n\nThen restart the service.", rank=2)

    context = ContextBuilder().build([first, second])

    assert context.text.count(shared) == 1
    assert "Then restart the service." in context.text


def test_a_chunk_that_is_entirely_a_repeat_is_dropped() -> None:
    shared = "Connection acquisition is not the database being slow."
    context = ContextBuilder().build([_hit("c1", shared), _hit("c2", shared, rank=2)])

    assert [s.chunk_id for s in context.sources] == ["c1"]
    assert context.dropped == ["c2"]


# ---------------------------------------------------------------------- empty ----


def test_no_chunks_is_an_empty_context_rather_than_a_header_alone() -> None:
    # "The knowledge base has nothing about this" is evidence. A header with nothing under it
    # reads to a model as an empty section it should fill in.
    context = ContextBuilder().build([])

    assert context.is_empty
    assert context.text == ""
