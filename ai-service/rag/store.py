"""Where chunks live, and how the dense half of retrieval searches them.

One implementation, PostgreSQL with pgvector, behind an interface — the same shape as the LLM
provider and for the same reason. The Qdrant option in docs/planning.md is a paragraph rather
than a package because there is nothing in this project a second vector database would make
better, but the seam is where it would go if there were.

The search is cosine distance over an HNSW index. Vectors arrive normalised, so cosine and
inner product would rank identically; cosine is used because that is the operator class the
index was built with, and a search whose operator disagrees with its index silently degrades
into a sequential scan that returns the right answer slowly.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, literal, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from rag.documents import Chunk, Document, RetrievedChunk, SourceType, StoredChunk
from rag.schema import document_chunks, documents, retrieval_logs

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpsertOutcome:
    """What an ingest did to one document."""

    document_id: str
    chunks_written: int
    replaced: bool


@dataclass(frozen=True, slots=True)
class StoreStats:
    """Enough to answer "is the knowledge base populated, and with what"."""

    documents: int
    chunks: int
    documents_by_type: dict[str, int]
    documents_by_service: dict[str, int]


class VectorStore(abc.ABC):
    """The chunk store, and the dense search over it."""

    @abc.abstractmethod
    async def upsert(
        self,
        document: Document,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> UpsertOutcome:
        """Write a document and its chunks, replacing any earlier version of it."""

    @abc.abstractmethod
    async def search(
        self,
        embedding: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        """The ``k`` nearest chunks to a query vector, nearest first."""

    @abc.abstractmethod
    async def all_chunks(self) -> list[StoredChunk]:
        """Every chunk, for building the in-memory lexical index."""

    @abc.abstractmethod
    async def content_hashes(self) -> dict[str, str]:
        """Document key to content hash, so an ingest can skip what has not changed."""

    @abc.abstractmethod
    async def delete_missing(self, keys: set[str]) -> int:
        """Remove indexed documents whose key is not in ``keys``. Returns how many went."""

    @abc.abstractmethod
    async def stats(self) -> StoreStats:
        """Counts, for the ingest report and the knowledge base page."""


def document_key(document: Document) -> str:
    """How a document is recognised as one that has been ingested before.

    Its path, for anything that came from a file. Its external id otherwise — a postmortem
    generated in Phase 9 has no file, but it does have the incident it belongs to, and
    regenerating it has to replace the old one rather than add a second.
    """
    key = document.path or document.external_id

    if not key:
        raise ValueError(
            f"Document '{document.title}' has neither a path nor an external id, "
            "so re-ingesting it could not replace it."
        )

    return key


class PgVectorStore(VectorStore):
    """The ``rag`` schema in the project's PostgreSQL instance."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, url: str, echo: bool = False) -> PgVectorStore:
        # pool_pre_ping because the AI service is a long-lived process against a database that
        # gets restarted by `docker compose down` more often than anything in production would.
        # Without it the first request after a restart fails on a dead connection.
        return cls(create_async_engine(url, echo=echo, pool_pre_ping=True))

    async def dispose(self) -> None:
        await self._engine.dispose()

    # ---------------------------------------------------------------- ingestion ----

    async def upsert(
        self,
        document: Document,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> UpsertOutcome:
        if len(chunks) != len(embeddings):
            raise ValueError(
                f"Got {len(chunks)} chunks and {len(embeddings)} embeddings for "
                f"'{document.title}'."
            )

        key = document_key(document)

        async with self._engine.begin() as connection:
            # Delete then insert, rather than update in place. The chunk count changes when a
            # document is edited, so an update would have to reconcile chunk indexes against
            # rows that may or may not still correspond — and the delete cascades, which makes
            # "the old chunks are gone" a property of the schema rather than of this function.
            existing = await connection.execute(
                select(documents.c.id).where(self._key_predicate(document, key))
            )
            previous = existing.scalar_one_or_none()

            if previous is not None:
                await connection.execute(delete(documents).where(documents.c.id == previous))

            inserted = await connection.execute(
                documents.insert().returning(documents.c.id),
                {
                    "source_type": document.source_type.value,
                    "title": document.title,
                    "service": document.service,
                    "external_id": document.external_id,
                    "path": document.path,
                    "content_hash": document.content_hash,
                    "metadata": document.metadata,
                },
            )
            document_id = inserted.scalar_one()

            if chunks:
                await connection.execute(
                    document_chunks.insert(),
                    [
                        {
                            "document_id": document_id,
                            "chunk_index": chunk.chunk_index,
                            "content": chunk.content,
                            "token_count": chunk.token_count,
                            "section": chunk.section,
                            "embedding": embedding,
                            "metadata": chunk.metadata,
                        }
                        for chunk, embedding in zip(chunks, embeddings, strict=True)
                    ],
                )

        return UpsertOutcome(
            document_id=str(document_id),
            chunks_written=len(chunks),
            replaced=previous is not None,
        )

    @staticmethod
    def _key_predicate(document: Document, key: str) -> Any:
        if document.path:
            return documents.c.path == key

        return (documents.c.external_id == key) & documents.c.path.is_(None)

    async def content_hashes(self) -> dict[str, str]:
        query = select(documents.c.path, documents.c.external_id, documents.c.content_hash)

        async with self._engine.connect() as connection:
            rows = await connection.execute(query)

        return {
            (row.path or row.external_id): row.content_hash
            for row in rows
            if row.path or row.external_id
        }

    async def delete_missing(self, keys: set[str]) -> int:
        """Remove documents whose source file is gone.

        Ingestion is a mirror of the corpus directory, not an append-only log: a runbook deleted
        from the repository has to leave the index too, or it keeps being retrieved as evidence
        for a system that no longer works that way.
        """
        async with self._engine.begin() as connection:
            rows = await connection.execute(
                select(documents.c.id, documents.c.path, documents.c.external_id)
            )
            stale = [
                row.id
                for row in rows
                if (key := row.path or row.external_id) and key not in keys
            ]

            if stale:
                await connection.execute(delete(documents).where(documents.c.id.in_(stale)))

        return len(stale)

    # ---------------------------------------------------------------- retrieval ----

    async def search(
        self,
        embedding: list[float],
        k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedChunk]:
        distance = document_chunks.c.embedding.cosine_distance(embedding)

        query = (
            select(*self._chunk_columns(), distance.label("distance"))
            .select_from(
                document_chunks.join(documents, document_chunks.c.document_id == documents.c.id)
            )
            .order_by(distance)
            .limit(k)
        )

        condition = metadata_condition(filters)

        if condition is not None:
            query = query.where(condition)

        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()

        return [
            RetrievedChunk(
                chunk=_to_stored_chunk(row),
                # Cosine distance is 0 for identical and 2 for opposite. Reported as similarity
                # because that is the direction every other score in the system runs, and a
                # ranking where lower is better is a bug waiting to be written by whoever reads
                # the number next.
                score=1.0 - float(row.distance),
                rank=rank,
                retriever="vector",
            )
            for rank, row in enumerate(rows, start=1)
        ]

    async def all_chunks(self) -> list[StoredChunk]:
        query = (
            select(*self._chunk_columns())
            .select_from(
                document_chunks.join(documents, document_chunks.c.document_id == documents.c.id)
            )
            .order_by(documents.c.id, document_chunks.c.chunk_index)
        )

        async with self._engine.connect() as connection:
            rows = (await connection.execute(query)).all()

        return [_to_stored_chunk(row) for row in rows]

    @staticmethod
    def _chunk_columns() -> list[Any]:
        return [
            document_chunks.c.id,
            document_chunks.c.document_id,
            document_chunks.c.content,
            document_chunks.c.chunk_index,
            document_chunks.c.section,
            document_chunks.c.metadata,
            documents.c.title,
            documents.c.source_type,
            documents.c.service,
            documents.c.external_id,
            documents.c.path,
        ]

    # -------------------------------------------------------------------- stats ----

    async def stats(self) -> StoreStats:
        async with self._engine.connect() as connection:
            document_count = await connection.scalar(select(func.count()).select_from(documents))
            chunk_count = await connection.scalar(
                select(func.count()).select_from(document_chunks)
            )
            by_type = (
                await connection.execute(
                    select(documents.c.source_type, func.count()).group_by(documents.c.source_type)
                )
            ).all()
            by_service = (
                await connection.execute(
                    select(documents.c.service, func.count())
                    .where(documents.c.service.is_not(None))
                    .group_by(documents.c.service)
                )
            ).all()

        return StoreStats(
            documents=int(document_count or 0),
            chunks=int(chunk_count or 0),
            documents_by_type={row[0]: row[1] for row in by_type},
            documents_by_service={row[0]: row[1] for row in by_service},
        )

    # ------------------------------------------------------------------ logging ----

    async def log_retrieval(
        self,
        query: str,
        retriever: str,
        filters: dict[str, Any],
        candidates: dict[str, list[str]],
        latency_ms: dict[str, int],
        investigation_id: str | None = None,
    ) -> None:
        """Record one search in ``rag.retrieval_logs``.

        Best effort. A search that answered is not failed because its audit row could not be
        written, and the caller is either an agent mid-investigation or a person on a page —
        neither of whom can do anything about a logging error.
        """
        try:
            async with self._engine.begin() as connection:
                await connection.execute(
                    retrieval_logs.insert(),
                    {
                        "investigation_id": UUID(investigation_id) if investigation_id else None,
                        "query": query,
                        "retriever": retriever,
                        "filters": filters,
                        "bm25_ids": candidates.get("bm25"),
                        "vector_ids": candidates.get("vector"),
                        "fused_ids": candidates.get("fused"),
                        "reranked_ids": candidates.get("reranked"),
                        "latency_ms": latency_ms,
                    },
                )
        except Exception as exc:  # noqa: BLE001 - logging must not fail the search
            logger.warning("Could not write a retrieval log row: %s", exc)


def metadata_condition(filters: dict[str, Any] | None) -> Any:
    """The SQL half of :mod:`rag.filters`.

    Written as JSONB containment rather than as ``metadata->>'key' = value`` so that the
    ``jsonb_path_ops`` GIN index on the column can be used; an any-of filter becomes an ``OR``
    of containments, which is still indexable. The values are text on both sides — see
    :func:`rag.filters.as_text` — so containment and Python equality agree.
    """
    if not filters:
        return None

    conditions = []

    for key, expected in filters.items():
        candidates = expected if isinstance(expected, list) else [expected]
        # A bound parameter, not an interpolated literal: filter values come from an HTTP
        # request, and the one place this code would be exposed to a query written by a caller
        # is exactly here.
        # The literal carries the JSONB type rather than being cast in SQL: asyncpg needs the
        # value serialised on the way out, and a bare `cast(...)` changes the SQL without telling
        # the driver what it is holding — which fails at execution with "'dict' object has no
        # attribute 'encode'".
        clauses = [
            document_chunks.c.metadata.op("@>", is_comparison=True)(
                literal({key: value}, JSONB)
            )
            for value in candidates
        ]
        conditions.append(or_(*clauses) if len(clauses) > 1 else clauses[0])

    return conditions[0] if len(conditions) == 1 else and_(*conditions)


def _to_stored_chunk(row: Any) -> StoredChunk:
    return StoredChunk(
        chunk_id=str(row.id),
        document_id=str(row.document_id),
        content=row.content,
        chunk_index=row.chunk_index,
        title=row.title,
        source_type=SourceType(row.source_type),
        service=row.service,
        external_id=row.external_id,
        path=row.path,
        section=row.section,
        metadata=dict(row.metadata or {}),
    )
