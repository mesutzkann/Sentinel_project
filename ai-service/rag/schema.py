"""The ``rag`` schema, as SQLAlchemy Core tables.

Core rather than the ORM. Nothing here needs identity mapping, lazy loading or a unit of work:
ingestion writes chunks in bulk and retrieval reads rows that are turned straight into
:class:`~rag.documents.StoredChunk`. An ORM layer over that would add a mapping to maintain and
take away control of the one query whose plan actually matters, the vector search.

This module is also what Alembic autogenerates against, so a column added here and not migrated
shows up as a diff rather than as a runtime error.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

SCHEMA = "rag"

# bge-m3's dense output. Declared on the column, so a provider configured for a different width
# fails at insert with a type error rather than filling the index with vectors that cannot be
# compared to each other.
EMBEDDING_DIMENSIONS = 1024

# Naming convention, so Alembic emits stable constraint names instead of letting PostgreSQL
# invent them. Without it, a migration that drops a constraint has to guess what it was called.
metadata = MetaData(
    schema=SCHEMA,
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s",
        "pk": "pk_%(table_name)s",
    },
)

_JSONB = JSON().with_variant(JSONB(), "postgresql")

documents = Table(
    "documents",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column("source_type", String(32), nullable=False),
    Column("title", Text, nullable=False),
    Column("service", String(64)),
    # The identifier the rest of the system already knows this by: INC-00023, a scenario code.
    Column("external_id", String(64)),
    # Repository-relative for anything loaded from a file, so a retrieved chunk can be traced
    # back to the file it came from without a search.
    Column("path", Text),
    Column("content_hash", String(64), nullable=False),
    Column("metadata", _JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("ingested_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Re-ingesting the same file has to replace the document rather than add a second copy of
    # it. Path is the natural key for a file; a document without one (a generated postmortem)
    # is identified by its external id instead, and the partial indexes below enforce whichever
    # applies without demanding both.
    Index(
        "uq_documents_path",
        "path",
        unique=True,
        postgresql_where=text("path IS NOT NULL"),
    ),
    Index(
        "uq_documents_external_id",
        "external_id",
        unique=True,
        postgresql_where=text("external_id IS NOT NULL AND path IS NULL"),
    ),
    Index("ix_documents_source_type", "source_type"),
    Index("ix_documents_service", "service"),
)

document_chunks = Table(
    "document_chunks",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    Column(
        "document_id",
        UUID(as_uuid=True),
        # A re-ingest deletes the document and writes it again; the chunks have to go with it,
        # and an orphaned chunk is a retrievable result whose source no longer exists.
        ForeignKey(f"{SCHEMA}.documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("chunk_index", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("token_count", Integer, nullable=False),
    Column("section", Text),
    Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
    Column("metadata", _JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_id"),
    Index(
        "ix_document_chunks_embedding",
        "embedding",
        postgresql_using="hnsw",
        # Cosine, matching the normalised vectors the embedding provider produces. The operator
        # class has to agree with the operator the query uses (<=>): pgvector will not use an
        # l2 index for a cosine search, and the search silently becomes a sequential scan.
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
    Index(
        "ix_document_chunks_metadata",
        "metadata",
        postgresql_using="gin",
        postgresql_ops={"metadata": "jsonb_path_ops"},
    ),
)

retrieval_logs = Table(
    "retrieval_logs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")),
    # Nullable: a search from the MCP Tools page or an evaluation run belongs to no
    # investigation, and those are exactly the searches worth comparing against.
    Column("investigation_id", UUID(as_uuid=True)),
    Column("query", Text, nullable=False),
    Column("retriever", String(32), nullable=False),
    Column("filters", _JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    # Each retriever's candidate list, kept separately. Which half proposed a chunk is the only
    # thing that explains a surprising hybrid result, and it cannot be reconstructed later.
    Column("bm25_ids", _JSONB),
    Column("vector_ids", _JSONB),
    Column("fused_ids", _JSONB),
    Column("reranked_ids", _JSONB),
    Column("latency_ms", _JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_retrieval_logs_investigation_id", "investigation_id"),
    Index("ix_retrieval_logs_created_at", "created_at"),
)
