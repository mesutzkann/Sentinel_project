"""Loading the corpus, and the properties that make re-running an ingest cheap and safe.

Idempotency is the one worth testing hardest. Ingestion is meant to be run on every start, and
that is only reasonable if a document whose content has not changed costs a hash comparison
rather than an embedding call — so the test counts the embedding calls rather than trusting the
report.
"""

from __future__ import annotations

import pytest

from rag.chunking import MarkdownChunker
from rag.documents import Chunk, Document, SourceType
from rag.embeddings import EmbeddingProvider
from rag.ingest import CorpusError, IngestionPipeline, load_corpus, parse_document
from rag.lexical import Bm25Index
from rag.store import StoreStats, UpsertOutcome, VectorStore, document_key

RUNBOOK = """---
type: runbook
title: Connection pool exhaustion
service: orders
id: RB-001
severity: high
---

# Connection pool exhaustion

Latency climbs to the client timeout ceiling and stays there.
"""


# ---------------------------------------------------------------- front matter ----


def test_front_matter_maps_onto_the_document() -> None:
    document = parse_document(RUNBOOK, path="runbooks/pool.md")

    assert document.source_type is SourceType.RUNBOOK
    assert document.title == "Connection pool exhaustion"
    assert document.service == "orders"
    assert document.external_id == "RB-001"
    assert document.path == "runbooks/pool.md"
    assert document.content.startswith("# Connection pool exhaustion")


def test_unreserved_keys_become_filterable_metadata() -> None:
    document = parse_document(RUNBOOK, path="runbooks/pool.md")

    assert document.metadata == {"severity": "high"}
    assert document.chunk_metadata()["severity"] == "high"


def test_the_title_falls_back_to_the_first_heading() -> None:
    text = "---\ntype: runbook\n---\n\n# Deadlocks in payments\n\nBody.\n"

    assert parse_document(text, path="x.md").title == "Deadlocks in payments"


def test_a_file_with_no_front_matter_is_rejected() -> None:
    with pytest.raises(CorpusError, match="front-matter"):
        parse_document("# Just a heading\n\nAnd a body.\n", path="x.md")


def test_an_unclosed_front_matter_block_is_rejected() -> None:
    with pytest.raises(CorpusError, match="never closed"):
        parse_document("---\ntype: runbook\ntitle: X\n\n# Body\n", path="x.md")


def test_an_unknown_type_is_rejected_and_says_what_is_valid() -> None:
    with pytest.raises(CorpusError, match="postmortem"):
        parse_document("---\ntype: blog_post\n---\n\n# Body\n", path="x.md")


def test_front_matter_with_no_body_is_rejected() -> None:
    with pytest.raises(CorpusError, match="no content"):
        parse_document("---\ntype: runbook\ntitle: X\n---\n\n   \n", path="x.md")


def test_a_malformed_line_names_itself() -> None:
    with pytest.raises(CorpusError, match="not `key: value`"):
        parse_document("---\ntype: runbook\njust-a-line\n---\n\n# Body\n", path="x.md")


# ------------------------------------------------------------- corpus loading ----


def test_loading_a_corpus_reports_bad_files_instead_of_failing(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "good.md").write_text(RUNBOOK, encoding="utf-8")
    (tmp_path / "bad.md").write_text("no front matter here", encoding="utf-8")

    documents, failures = load_corpus(tmp_path)

    # One bad runbook costs that runbook, not the other twenty-nine — but it is reported, not
    # skipped quietly.
    assert [d.title for d in documents] == ["Connection pool exhaustion"]
    assert [f.key for f in failures] == ["bad.md"]
    assert failures[0].status == "failed"


def test_underscore_files_are_not_part_of_the_corpus(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "_README.md").write_text("# How this directory works\n", encoding="utf-8")
    (tmp_path / "good.md").write_text(RUNBOOK, encoding="utf-8")

    documents, failures = load_corpus(tmp_path)

    assert len(documents) == 1
    assert failures == []


def test_paths_are_relative_so_they_survive_a_different_checkout(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "runbooks").mkdir()
    (tmp_path / "runbooks" / "pool.md").write_text(RUNBOOK, encoding="utf-8")

    documents, _ = load_corpus(tmp_path)

    assert documents[0].path == "runbooks/pool.md"


def test_a_missing_corpus_directory_is_an_error(tmp_path) -> None:  # noqa: ANN001
    with pytest.raises(CorpusError, match="does not exist"):
        load_corpus(tmp_path / "nowhere")


# -------------------------------------------------------------------- pipeline ----


class _CountingEmbeddings(EmbeddingProvider):
    def __init__(self) -> None:
        self.calls = 0
        self.texts = 0

    @property
    def model(self) -> str:
        return "counting"

    @property
    def dimensions(self) -> int:
        return 3

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts += len(texts)

        return [[1.0, 0.0, 0.0] for _ in texts]

    async def is_available(self) -> bool:
        return True


class _MemoryStore(VectorStore):
    """Enough of a store to test the pipeline's decisions without a database."""

    def __init__(self) -> None:
        self.documents: dict[str, tuple[Document, list[Chunk]]] = {}

    async def upsert(
        self,
        document: Document,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> UpsertOutcome:
        key = document_key(document)
        replaced = key in self.documents
        self.documents[key] = (document, chunks)

        return UpsertOutcome(document_id=key, chunks_written=len(chunks), replaced=replaced)

    async def search(self, embedding, k, filters=None):  # noqa: ANN001, ANN201, ARG002
        return []

    async def all_chunks(self):  # noqa: ANN201
        return []

    async def content_hashes(self) -> dict[str, str]:
        return {k: d.content_hash for k, (d, _) in self.documents.items()}

    async def delete_missing(self, keys: set[str]) -> int:
        stale = set(self.documents) - keys

        for key in stale:
            del self.documents[key]

        return len(stale)

    async def stats(self) -> StoreStats:
        return StoreStats(len(self.documents), 0, {}, {})


def _pipeline() -> tuple[IngestionPipeline, _CountingEmbeddings, _MemoryStore]:
    embeddings = _CountingEmbeddings()
    store = _MemoryStore()
    pipeline = IngestionPipeline(MarkdownChunker(), embeddings, store, Bm25Index())

    return pipeline, embeddings, store


async def test_an_unchanged_document_is_not_re_embedded(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "pool.md").write_text(RUNBOOK, encoding="utf-8")
    pipeline, embeddings, _ = _pipeline()

    first = await pipeline.ingest_corpus(tmp_path)
    calls_after_first = embeddings.calls
    second = await pipeline.ingest_corpus(tmp_path)

    assert first.count("ingested") == 1
    assert second.count("unchanged") == 1
    assert embeddings.calls == calls_after_first, "the second run embedded something"


async def test_an_edited_document_replaces_the_old_one(tmp_path) -> None:  # noqa: ANN001
    file = tmp_path / "pool.md"
    file.write_text(RUNBOOK, encoding="utf-8")
    pipeline, _, store = _pipeline()

    await pipeline.ingest_corpus(tmp_path)
    file.write_text(RUNBOOK + "\n## Prevent\n\nAlert on connections above 80%.\n", encoding="utf-8")
    report = await pipeline.ingest_corpus(tmp_path)

    assert report.count("replaced") == 1
    assert len(store.documents) == 1, "the edit added a second copy instead of replacing"


async def test_force_re_embeds_an_unchanged_document(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "pool.md").write_text(RUNBOOK, encoding="utf-8")
    pipeline, embeddings, _ = _pipeline()

    await pipeline.ingest_corpus(tmp_path)
    before = embeddings.calls
    report = await pipeline.ingest_corpus(tmp_path, force=True)

    assert report.count("replaced") == 1
    assert embeddings.calls > before


async def test_a_deleted_file_leaves_the_index(tmp_path) -> None:  # noqa: ANN001
    # A runbook deleted from the repository should stop being citable evidence.
    (tmp_path / "pool.md").write_text(RUNBOOK, encoding="utf-8")
    (tmp_path / "leak.md").write_text(
        RUNBOOK.replace("RB-001", "RB-002").replace("Connection pool exhaustion", "Memory leak"),
        encoding="utf-8",
    )
    pipeline, _, store = _pipeline()

    await pipeline.ingest_corpus(tmp_path)
    (tmp_path / "leak.md").unlink()
    report = await pipeline.ingest_corpus(tmp_path)

    assert report.count("removed") == 1
    assert set(store.documents) == {"pool.md"}


async def test_pruning_is_off_for_a_targeted_ingest() -> None:
    # Phase 9 ingests one generated postmortem at a time; that must not empty the corpus.
    pipeline, _, store = _pipeline()
    existing = parse_document(RUNBOOK, path="runbooks/pool.md")

    await pipeline.ingest([existing])
    generated = Document(
        source_type=SourceType.POSTMORTEM,
        title="INC-1",
        content="Body.",
        external_id="INC-1",
    )

    await pipeline.ingest([generated])

    assert set(store.documents) == {"runbooks/pool.md", "INC-1"}


async def test_a_bad_file_is_reported_in_the_ingest_result(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "good.md").write_text(RUNBOOK, encoding="utf-8")
    (tmp_path / "bad.md").write_text("nothing useful", encoding="utf-8")
    pipeline, _, _ = _pipeline()

    report = await pipeline.ingest_corpus(tmp_path)

    assert report.count("ingested") == 1
    assert report.count("failed") == 1
    assert report.failed[0].error


async def test_the_lexical_index_is_rebuilt_after_an_ingest(tmp_path) -> None:  # noqa: ANN001
    (tmp_path / "pool.md").write_text(RUNBOOK, encoding="utf-8")
    embeddings = _CountingEmbeddings()
    store = _MemoryStore()
    index = Bm25Index()
    rebuilds: list[int] = []

    original_build = index.build

    def counting_build(chunks):  # noqa: ANN001, ANN202
        rebuilds.append(len(chunks))
        original_build(chunks)

    index.build = counting_build  # type: ignore[method-assign]

    await IngestionPipeline(MarkdownChunker(), embeddings, store, index).ingest_corpus(tmp_path)

    # Once, at the end — not once per document, which would be quadratic in the corpus size.
    assert len(rebuilds) == 1


async def test_a_document_with_no_key_is_rejected() -> None:
    pipeline, _, _ = _pipeline()
    orphan = Document(source_type=SourceType.RUNBOOK, title="No key", content="Body.")

    with pytest.raises(ValueError, match="neither a path nor an external id"):
        await pipeline.ingest([orphan])
