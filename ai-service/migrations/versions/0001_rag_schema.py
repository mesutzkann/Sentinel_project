"""The rag schema: documents, chunks with embeddings, and retrieval logs.

Revision ID: 0001
Revises:
Created: Phase 5

The schema itself already exists — infrastructure/postgres/init/01-extensions-and-schemas.sql
creates it, along with the `vector` extension, on the database's first start. This migration
creates the tables inside it and nothing else, so it stays runnable against a database the
backend is already using.

One deliberate omission from the DDL sketched in docs/planning.md: there is no generated
`tsvector` column. Lexical search is BM25 held in memory (see rag/lexical.py), which is a better
match for a corpus of a few hundred chunks and lets the ranking function be the one this project
chose rather than PostgreSQL's. A `tsvector` column and its GIN index are the fallback if the
corpus ever outgrows the process, and an unused index that has to be kept correct in the
meantime is a cost with no reader.
"""

from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMA = "rag"
EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=True),
        sa.Column("external_id", sa.String(length=64), nullable=True),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_documents"),
        schema=SCHEMA,
    )

    # Partial, because the natural key differs by origin: a file is identified by its path, and
    # a document generated at runtime — a Phase 9 postmortem — by the incident it belongs to.
    op.create_index(
        "uq_documents_path",
        "documents",
        ["path"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("path IS NOT NULL"),
    )
    op.create_index(
        "uq_documents_external_id",
        "documents",
        ["external_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("external_id IS NOT NULL AND path IS NULL"),
    )
    op.create_index("ix_documents_source_type", "documents", ["source_type"], schema=SCHEMA)
    op.create_index("ix_documents_service", "documents", ["service"], schema=SCHEMA)

    op.create_table(
        "document_chunks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.Vector(EMBEDDING_DIMENSIONS),
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            [f"{SCHEMA}.documents.id"],
            name="fk_document_chunks_document_id",
            # Re-ingesting replaces the document row; its chunks have to go with it, and an
            # orphaned chunk is a retrievable result whose source no longer exists.
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_document_chunks"),
        sa.UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_id"
        ),
        schema=SCHEMA,
    )

    # HNSW rather than IVFFlat: it needs no training pass over existing data, which matters for
    # an index that starts empty and is built up by ingestion. Cosine ops, matching both the
    # normalised vectors and the `<=>` operator the store's query uses.
    op.create_index(
        "ix_document_chunks_embedding",
        "document_chunks",
        ["embedding"],
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # jsonb_path_ops: smaller and faster than the default operator class, and containment (`@>`)
    # is the only operator the metadata filter uses.
    op.create_index(
        "ix_document_chunks_metadata",
        "document_chunks",
        ["metadata"],
        schema=SCHEMA,
        postgresql_using="gin",
        postgresql_ops={"metadata": "jsonb_path_ops"},
    )

    op.create_table(
        "retrieval_logs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("retriever", sa.String(length=32), nullable=False),
        sa.Column(
            "filters",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("bm25_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("vector_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fused_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reranked_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "latency_ms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_retrieval_logs"),
        schema=SCHEMA,
    )

    # No foreign key to the investigation: it lives in the `sentinel` schema, which the backend
    # owns. A cross-schema constraint would make one service's migrations depend on the other's
    # ordering for a link that is only ever read, never enforced.
    op.create_index(
        "ix_retrieval_logs_investigation_id",
        "retrieval_logs",
        ["investigation_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_retrieval_logs_created_at", "retrieval_logs", ["created_at"], schema=SCHEMA
    )


def downgrade() -> None:
    op.drop_table("retrieval_logs", schema=SCHEMA)
    op.drop_table("document_chunks", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
