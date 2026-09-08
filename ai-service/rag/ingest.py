"""Getting the knowledge base from files into the index.

The corpus is Markdown on disk with a small front-matter block naming what each file is. That is
deliberate: a runbook is a document a person maintains, and the moment its metadata lives in a
database rather than in the file, the file and the index start to disagree about what the file
says.

Ingestion is a mirror, not an append: a file that has not changed is skipped without being
re-embedded, and a file that has been deleted is removed from the index. Both matter for the
same reason — the knowledge base is evidence the agent cites, and evidence that no longer exists
in the repository should not be citable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag.chunking import MarkdownChunker
from rag.documents import Document, SourceType
from rag.embeddings import EmbeddingProvider
from rag.lexical import LexicalIndex
from rag.store import VectorStore, document_key

logger = logging.getLogger(__name__)

_FRONT_MATTER_FENCE = "---"

# Front-matter keys that map onto columns rather than into the free-form metadata blob.
_RESERVED = {"type", "title", "service", "id"}


class CorpusError(ValueError):
    """A file in the corpus cannot be read as a document."""


@dataclass(frozen=True, slots=True)
class DocumentOutcome:
    """What happened to one document in an ingest run."""

    key: str
    title: str
    status: str  # ingested | replaced | unchanged | removed | failed
    chunks: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class IngestReport:
    """The result of an ingest run, in the shape the endpoint returns it."""

    documents: list[DocumentOutcome] = field(default_factory=list)
    duration_ms: int = 0

    def count(self, status: str) -> int:
        return sum(1 for d in self.documents if d.status == status)

    @property
    def chunks_written(self) -> int:
        return sum(d.chunks for d in self.documents)

    @property
    def failed(self) -> list[DocumentOutcome]:
        return [d for d in self.documents if d.status == "failed"]


def parse_document(text: str, path: str | None = None) -> Document:
    """Read one Markdown file with front matter into a :class:`Document`.

    The front matter is a flat ``key: value`` block between ``---`` fences — not YAML, and not
    parsed with a YAML library. The corpus needs six keys and no nesting; a YAML dependency
    would buy anchors, multi-line scalars and type coercion that this format has no use for, and
    the failure mode of a real YAML parser on a hand-edited file ("expected <block end>") is
    worse than the one below.

    Raises:
        CorpusError: the file has no front matter, or names a type that does not exist.
    """
    front_matter, body = _split_front_matter(text, path)

    if "type" not in front_matter:
        raise CorpusError(f"{path or 'document'} has no `type:` in its front matter.")

    try:
        source_type = SourceType(front_matter["type"])
    except ValueError as exc:
        valid = ", ".join(sorted(t.value for t in SourceType))
        raise CorpusError(
            f"{path or 'document'} has type '{front_matter['type']}', which is not one of: {valid}"
        ) from exc

    if not body.strip():
        raise CorpusError(f"{path or 'document'} has front matter but no content.")

    title = front_matter.get("title") or _first_heading(body) or (path or "Untitled")

    return Document(
        source_type=source_type,
        title=title,
        content=body.strip(),
        service=front_matter.get("service"),
        external_id=front_matter.get("id"),
        path=path,
        metadata={k: v for k, v in front_matter.items() if k not in _RESERVED},
    )


def load_corpus(root: Path) -> tuple[list[Document], list[DocumentOutcome]]:
    """Every Markdown file under ``root``, and the ones that could not be read.

    A malformed file does not stop the ingest. One unparseable runbook should cost that runbook,
    not the other twenty-nine — but it is reported rather than skipped quietly, because a
    document silently missing from the knowledge base looks exactly like a knowledge base that
    has nothing to say.
    """
    documents: list[Document] = []
    failures: list[DocumentOutcome] = []

    if not root.is_dir():
        raise CorpusError(f"The knowledge base directory does not exist: {root}")

    for file in sorted(root.rglob("*.md")):
        # A leading underscore marks a file that documents the corpus rather than belonging to
        # it. Without the convention, the directory's own README is either ingested as a runbook
        # or reported as a failure on every single run.
        if file.name.startswith("_"):
            continue

        relative = file.relative_to(root).as_posix()

        try:
            documents.append(parse_document(file.read_text(encoding="utf-8"), path=relative))
        except (CorpusError, OSError, UnicodeDecodeError) as exc:
            logger.warning("Skipping %s: %s", relative, exc)
            failures.append(
                DocumentOutcome(key=relative, title=relative, status="failed", error=str(exc))
            )

    return documents, failures


class IngestionPipeline:
    """Chunk, embed, store, and rebuild the lexical index."""

    def __init__(
        self,
        chunker: MarkdownChunker,
        embeddings: EmbeddingProvider,
        store: VectorStore,
        lexical: LexicalIndex,
    ) -> None:
        self._chunker = chunker
        self._embeddings = embeddings
        self._store = store
        self._lexical = lexical

    async def ingest(
        self,
        documents: list[Document],
        force: bool = False,
        prune: bool = False,
        failures: list[DocumentOutcome] | None = None,
    ) -> IngestReport:
        """Ingest a set of documents, skipping what has not changed.

        ``prune`` removes indexed documents that are not in this set, which is what makes a
        corpus ingest a mirror of the directory. It is off by default: ingesting a single
        generated postmortem must not delete the other 30 documents, and that is the call this
        method will mostly be given in Phase 9.
        """
        started = time.perf_counter()
        outcomes: list[DocumentOutcome] = list(failures or [])
        existing = await self._store.content_hashes()

        for document in documents:
            outcomes.append(await self._ingest_one(document, existing, force))

        if prune:
            keys = {document_key(d) for d in documents}
            removed = await self._store.delete_missing(keys)

            outcomes.extend(
                DocumentOutcome(key=key, title=key, status="removed")
                for key in sorted(set(existing) - keys)
            )

            if removed != len(set(existing) - keys):
                # The store and this loop disagreeing means something wrote to the schema
                # underneath the ingest. Worth a line in the log, not worth failing over.
                logger.warning(
                    "Pruned %d documents but expected %d.", removed, len(set(existing) - keys)
                )

        # Once, at the end. The lexical index is rebuilt wholesale, so doing it per document
        # would be quadratic in the size of the corpus for no benefit anyone can observe.
        await self.rebuild_lexical_index()

        return IngestReport(
            documents=outcomes,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _ingest_one(
        self,
        document: Document,
        existing: dict[str, str],
        force: bool,
    ) -> DocumentOutcome:
        key = document_key(document)
        known = existing.get(key)

        if not force and known == document.content_hash:
            return DocumentOutcome(key=key, title=document.title, status="unchanged")

        chunks = self._chunker.chunk(document)

        if not chunks:
            return DocumentOutcome(
                key=key,
                title=document.title,
                status="failed",
                error="The document produced no chunks.",
            )

        embeddings = await self._embeddings.embed([c.content for c in chunks])
        outcome = await self._store.upsert(document, chunks, embeddings)

        return DocumentOutcome(
            key=key,
            title=document.title,
            status="replaced" if outcome.replaced else "ingested",
            chunks=outcome.chunks_written,
        )

    async def ingest_corpus(
        self,
        root: Path,
        force: bool = False,
        prune: bool = True,
    ) -> IngestReport:
        """Ingest every Markdown file under ``root`` and remove what is no longer there."""
        documents, failures = load_corpus(root)

        return await self.ingest(documents, force=force, prune=prune, failures=failures)

    async def rebuild_lexical_index(self) -> int:
        """Reload BM25 from the store. Called after every ingest and once at startup."""
        chunks = await self._store.all_chunks()
        self._lexical.build(chunks)

        logger.info("Lexical index rebuilt over %d chunks.", len(chunks))

        return len(chunks)


def _split_front_matter(text: str, path: str | None) -> tuple[dict[str, Any], str]:
    lines = text.lstrip("﻿").splitlines()

    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        raise CorpusError(
            f"{path or 'document'} does not start with a `---` front-matter block."
        )

    front_matter: dict[str, Any] = {}

    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONT_MATTER_FENCE:
            return front_matter, "\n".join(lines[index + 1 :])

        if not line.strip() or line.lstrip().startswith("#"):
            continue

        key, separator, value = line.partition(":")

        if not separator:
            raise CorpusError(f"{path or 'document'}: '{line.strip()}' is not `key: value`.")

        front_matter[key.strip()] = value.strip()

    raise CorpusError(f"{path or 'document'}: the front-matter block is never closed.")


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()

    return None
