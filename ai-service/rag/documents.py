"""What goes into the knowledge base, and what comes back out of it.

The types here are deliberately free of storage concerns: a :class:`Document` is what a loader
produces from a file, a :class:`Chunk` is what the chunker produces from a document, and neither
knows that PostgreSQL exists. That is what lets the chunker and the retrievers be tested without
a database, and what would let the store be swapped for Qdrant without touching either.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SourceType(StrEnum):
    """Where a document came from.

    Fixed rather than free text because it is a retrieval filter, not a label: "search the
    runbooks" and "search past incidents" are different questions, and the agent asks both. The
    set matches ``rag.documents.source_type`` in docs/planning.md.
    """

    INCIDENT = "incident"
    RUNBOOK = "runbook"
    ARCHITECTURE = "architecture"
    SERVICE_DOC = "service_doc"
    CODE_DOC = "code_doc"
    POSTMORTEM = "postmortem"
    DEPLOYMENT_DOC = "deployment_doc"
    KNOWN_ERROR = "known_error"


@dataclass(frozen=True, slots=True)
class Document:
    """One source file, before it is chunked.

    ``external_id`` is the identifier the rest of the system already knows the thing by —
    ``INC-00023`` for an incident, a scenario code for a known error. It is what makes an
    ingested postmortem findable from an incident row without a join through content.
    """

    source_type: SourceType
    title: str
    content: str
    service: str | None = None
    external_id: str | None = None
    path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """SHA-256 of the content, used to skip re-embedding a document that has not changed.

        Over the content only. The title and metadata live in ``rag.documents``, which is
        rewritten on every ingest anyway; embedding is the expensive part and it depends on
        nothing else.
        """
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def chunk_metadata(self) -> dict[str, Any]:
        """The document-level fields every chunk of it carries.

        Copied onto the chunk rather than joined at query time because the vector search filters
        on them: ``WHERE metadata @> '{"service": "orders"}'`` uses the GIN index on the chunk
        row, and a join to ``documents`` to reach the same value would not.
        """
        inherited = {
            "document_type": self.source_type.value,
            "service": self.service,
            "external_id": self.external_id,
            "path": self.path,
            **self.metadata,
        }

        # Values are stored as text, deliberately. This map exists to be filtered on, and the
        # filter is evaluated in two places — in Python against a dictionary and in SQL against
        # a JSONB column. A severity of 3 that is a number in one and the string "3" in the
        # other is the kind of mismatch that returns fewer results rather than an error, so the
        # type is settled here, once, at the boundary where the values are written.
        #
        # A null is dropped rather than stored: in JSONB it is a value, so a chunk with
        # {"service": null} carries a key that has to be reasoned about and matches nothing.
        return {k: _as_text(v) for k, v in inherited.items() if v is not None}


def _as_text(value: Any) -> str:
    """One scalar, as the text form both the JSONB column and the filter comparison use.

    Booleans go to ``true``/``false`` rather than Python's ``True``/``False``: the value is
    round-tripped through JSON, and JSON spells them lowercase.
    """
    if isinstance(value, bool):
        return "true" if value else "false"

    return str(value)


@dataclass(frozen=True, slots=True)
class Chunk:
    """A slice of a document, sized for an embedding model and for a prompt.

    ``section`` is the heading path the slice sits under (``Runbook > Diagnosis > Metrics``).
    The chunker keeps it because a chunk taken out of a long runbook is close to unreadable
    without it — both for the model and for whoever is reading the evidence panel.
    """

    content: str
    chunk_index: int
    token_count: int
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StoredChunk:
    """A chunk as it exists in the index: identified, and carrying its document's identity.

    The document fields are denormalised onto it because every consumer needs them and none of
    them wants a second query: the lexical index is in memory and has no join to make, and a
    retrieval result has to be renderable as "runbook, orders, section" without another round
    trip.
    """

    chunk_id: str
    document_id: str
    content: str
    chunk_index: int
    title: str
    source_type: SourceType
    service: str | None = None
    external_id: str | None = None
    path: str | None = None
    section: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A stored chunk, with the position a retriever gave it.

    ``score`` is only comparable within one retriever: BM25 term weights, cosine similarity and
    reciprocal rank fusion are three different scales. ``rank`` is comparable across all of
    them, which is why the Phase 6 evaluation is written against rank rather than score.
    """

    chunk: StoredChunk
    score: float
    rank: int
    retriever: str

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id

    @property
    def content(self) -> str:
        return self.chunk.content

    def reranked(self, rank: int, score: float, retriever: str) -> RetrievedChunk:
        """The same chunk at a new position, which is what fusion and reranking produce."""
        return RetrievedChunk(chunk=self.chunk, score=score, rank=rank, retriever=retriever)
