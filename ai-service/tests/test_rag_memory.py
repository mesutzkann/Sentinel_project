"""Storing a postmortem: the corpus and the index, and why both.

The failure this file guards against is subtle and total. A postmortem ingested into the `rag`
schema but never written to disk survives until the next `POST /rag/ingest`, which mirrors the
knowledge base directory and prunes what is not in it — and then the memory is gone, silently,
at the moment somebody re-ingests the corpus for an unrelated reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agents.context import EvidenceItem, EvidenceSource, RootCause
from agents.postmortem import PostmortemDraft, build
from rag.ingest import DocumentOutcome, IngestReport, parse_document
from rag.memory import IncidentMemory
from tests.support import context

DRAFT = PostmortemDraft(
    summary="Orders timed out after a configuration change cut the connection pool to 20.",
    what_we_saw="Latency reached 4.2 s at p95 and the logs carried `Timeout waiting for pool`.",
    lessons=["A pool size is a capacity limit."],
)


class FakePipeline:
    """Records what it was asked to ingest, with the flags it was asked with."""

    def __init__(self, status: str = "ingested", raises: Exception | None = None) -> None:
        self.calls: list[tuple[list, bool]] = []
        self._status = status
        self._raises = raises

    async def ingest(self, documents, force=False, prune=False, failures=None):
        self.calls.append((documents, prune))

        if self._raises is not None:
            raise self._raises

        return IngestReport(
            documents=[
                DocumentOutcome(
                    key=d.path or d.title, title=d.title, status=self._status, chunks=4
                )
                for d in documents
            ],
            duration_ms=12,
        )


def postmortem():
    ctx = context()
    ctx.root_cause = RootCause(
        title="Connection pool exhausted on orders",
        explanation="The pool was cut from 200 to 20, so requests queued and timed out.",
        category="DB_CONNECTION_POOL_EXHAUSTION",
        confidence=0.78,
    )
    ctx.evidence = [
        EvidenceItem(source=EvidenceSource.METRICS, summary="p95 latency 4.2s", weight=0.8)
    ]

    return build(ctx, DRAFT, duration_ms=120_000)


@pytest.mark.asyncio
async def test_the_file_lands_in_the_corpus_and_parses_as_a_document(tmp_path: Path) -> None:
    """Written where the loader will find it, in a form the loader accepts."""
    pipeline = FakePipeline()
    remembered = await IncidentMemory(pipeline, tmp_path).remember(postmortem())

    assert remembered.written and remembered.indexed
    assert remembered.path.is_relative_to(tmp_path)
    assert remembered.path.exists()

    reloaded = parse_document(remembered.path.read_text(encoding="utf-8"), path="x.md")

    assert reloaded.external_id == "INC-00142"
    assert reloaded.title.startswith("INC-00142")


@pytest.mark.asyncio
async def test_it_never_prunes(tmp_path: Path) -> None:
    """One document goes in; the other thirty are not absent, they are simply not in this call."""
    pipeline = FakePipeline()
    await IncidentMemory(pipeline, tmp_path).remember(postmortem())

    documents, prune = pipeline.calls[0]

    assert prune is False
    assert len(documents) == 1


@pytest.mark.asyncio
async def test_a_regenerated_postmortem_overwrites_its_own_file(tmp_path: Path) -> None:
    """Idempotent by content: the same incident does not accumulate files."""
    memory = IncidentMemory(FakePipeline(status="unchanged"), tmp_path)

    first = await memory.remember(postmortem())
    second = await memory.remember(postmortem())

    assert first.path == second.path
    assert len(list(tmp_path.rglob("*.md"))) == 1
    assert second.indexed, "an unchanged document is still in the index"


@pytest.mark.asyncio
async def test_an_index_that_will_not_take_it_still_leaves_the_file(tmp_path: Path) -> None:
    """The embedding model being down must not cost the write-up as well as the indexing."""
    pipeline = FakePipeline(raises=RuntimeError("bge-m3 is not loaded"))
    remembered = await IncidentMemory(pipeline, tmp_path).remember(postmortem())

    assert remembered.written is True
    assert remembered.indexed is False
    assert remembered.status == "written_not_indexed"
    assert "bge-m3" in (remembered.error or "")
    assert remembered.path.exists(), "the next corpus ingest can still pick it up"


@pytest.mark.asyncio
async def test_a_directory_it_cannot_write_to_is_reported_not_raised(tmp_path: Path) -> None:
    """An investigation that reached a conclusion has produced its value already."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    remembered = await IncidentMemory(FakePipeline(), blocked).remember(postmortem())

    assert remembered.written is False
    assert remembered.indexed is False
    assert remembered.status == "failed"
    assert remembered.error
