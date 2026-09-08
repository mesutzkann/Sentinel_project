"""The chunker.

Every test here is about a chunk being readable on its own, because that is the only property
that matters downstream: a chunk is retrieved without its document, embedded without its
document, and shown to a person without its document.
"""

from __future__ import annotations

import pytest

from rag.chunking import MarkdownChunker, estimate_tokens
from rag.documents import Document, SourceType

RUNBOOK = """# Connection pool exhaustion

## Symptom

Latency climbs to the client timeout ceiling and stays there. Error rate rises and the errors
are timeouts rather than exceptions from the database itself.

## Confirm it

Active connections pinned at the pool maximum rather than fluctuating below it.

```bash
update_env_and_restart orders ConnectionStrings__Postgres "...;MaxPoolSize=200"
```

## Prevent

Alert on active connections above 80% of MaxPoolSize for five minutes.
"""


def _document(content: str, title: str = "Connection pool exhaustion") -> Document:
    return Document(
        source_type=SourceType.RUNBOOK,
        title=title,
        content=content,
        service="orders",
        path="runbooks/pool.md",
    )


def test_a_short_document_is_one_chunk() -> None:
    chunks = MarkdownChunker().chunk(_document(RUNBOOK))

    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0


def test_an_empty_document_produces_no_chunks() -> None:
    # Rather than one empty chunk, which is retrievable and unreadable.
    assert MarkdownChunker().chunk(_document("   \n\n  ")) == []


def test_chunks_carry_the_heading_they_sit_under() -> None:
    chunker = MarkdownChunker(target_tokens=40, overlap_tokens=10)
    chunks = chunker.chunk(_document(RUNBOOK))

    sections = [c.section for c in chunks]

    assert any(s and "Symptom" in s for s in sections)
    assert any(s and "Prevent" in s for s in sections)


def test_a_chunk_that_does_not_start_with_its_heading_is_given_a_breadcrumb() -> None:
    chunker = MarkdownChunker(target_tokens=40, overlap_tokens=10)
    chunks = chunker.chunk(_document(RUNBOOK))

    # "Alert on active connections..." means nothing on its own; the breadcrumb is what tells a
    # reader — and an embedding — which runbook and which step it belongs to.
    prose = [c for c in chunks if not c.content.startswith("#")]

    assert prose, "expected at least one chunk that does not begin with a heading"
    assert all(c.content.startswith("Connection pool exhaustion") for c in prose)


def test_the_breadcrumb_can_be_turned_off() -> None:
    chunker = MarkdownChunker(target_tokens=40, overlap_tokens=10, prepend_context=False)

    chunks = chunker.chunk(_document(RUNBOOK))

    assert not any(c.content.startswith("Connection pool exhaustion ·") for c in chunks)


def test_a_code_fence_is_never_split() -> None:
    chunker = MarkdownChunker(target_tokens=30, overlap_tokens=5)
    chunks = chunker.chunk(_document(RUNBOOK))

    fenced = [c for c in chunks if "```" in c.content]

    assert fenced, "expected the code fence to land in a chunk"
    for chunk in fenced:
        assert chunk.content.count("```") % 2 == 0, "a fence was left unclosed"


def test_consecutive_chunks_overlap() -> None:
    chunker = MarkdownChunker(target_tokens=40, overlap_tokens=20)
    chunks = chunker.chunk(_document(RUNBOOK))

    assert len(chunks) > 2

    # Overlap is whole blocks, so a repeated block appears verbatim in both chunks rather than
    # as a half sentence in each.
    overlaps = [
        any(block and block in chunks[i + 1].content for block in chunks[i].content.split("\n\n"))
        for i in range(len(chunks) - 1)
    ]

    assert any(overlaps)


def test_no_chunk_repeats_only_what_the_previous_one_said() -> None:
    chunker = MarkdownChunker(target_tokens=40, overlap_tokens=20)
    chunks = chunker.chunk(_document(RUNBOOK))

    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current.content.strip() not in previous.content


def test_a_paragraph_longer_than_a_chunk_is_split_by_sentence() -> None:
    long_paragraph = " ".join(f"Sentence number {i} explains one more thing." for i in range(80))
    chunks = MarkdownChunker(target_tokens=60, overlap_tokens=10).chunk(_document(long_paragraph))

    assert len(chunks) > 1
    assert all(c.token_count <= 120 for c in chunks)


def test_a_bulleted_list_stays_in_one_block() -> None:
    content = "# Steps\n\nDo these in order:\n\n- First\n- Second\n- Third\n"
    chunks = MarkdownChunker(target_tokens=400, overlap_tokens=60).chunk(_document(content))

    assert len(chunks) == 1
    assert "- First\n- Second\n- Third" in chunks[0].content


def test_chunk_metadata_comes_from_the_document() -> None:
    document = Document(
        source_type=SourceType.POSTMORTEM,
        title="INC-00001",
        content=RUNBOOK,
        service="orders",
        external_id="INC-00001",
        path="incidents/inc-00001.md",
        metadata={"severity": "high", "duration_minutes": 47},
    )

    chunk = MarkdownChunker().chunk(document)[0]

    assert chunk.metadata["document_type"] == "postmortem"
    assert chunk.metadata["service"] == "orders"
    assert chunk.metadata["external_id"] == "INC-00001"
    # Stored as text, so the SQL filter and the in-memory filter agree about what it equals.
    assert chunk.metadata["duration_minutes"] == "47"


def test_overlap_larger_than_the_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlap_tokens"):
        MarkdownChunker(target_tokens=100, overlap_tokens=100)


def test_token_estimates_scale_with_length() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("short") >= 1
    assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)
