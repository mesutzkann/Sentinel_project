"""PgVectorStore against a real PostgreSQL, skipped when there is not one.

Two things can only be established here. The first is that the schema behaves as designed —
that replacing a document takes its chunks with it, that the vector index is actually searched,
that a 1024-dimension column rejects anything else. The second, and the reason this file exists
at all, is that the SQL metadata filter and the Python one agree: they are two implementations
of one paragraph in ``rag/filters.py``, and a disagreement between them shows up in production
as a search that quietly returns three results instead of five.

Documents written here use a ``tests/`` path prefix and are removed afterwards, so the file is
safe to run against the development database with the real corpus in it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from app.config import settings
from rag.documents import Chunk, Document, SourceType
from rag.filters import matches, normalize
from rag.schema import document_chunks, documents
from rag.store import PgVectorStore

DIMENSIONS = 1024
PREFIX = "tests/rag-store/"

# Every document written here carries this marker, and every search here filters on it. The
# development database holds the real 28-document corpus, so a search for "service = orders"
# that did not exclude it would come back full of real runbooks and push the test's own
# documents past k — which is a test that fails for a reason that has nothing to do with the
# code under test.
MARKER = {"corpus": "rag-store-test"}


def _vector(seed: int) -> list[float]:
    """A deterministic unit vector, so distances are reproducible without an embedding model."""
    vector = [0.0] * DIMENSIONS
    vector[seed % DIMENSIONS] = 1.0

    return vector


def _document(name: str, service: str, source_type: SourceType = SourceType.RUNBOOK) -> Document:
    return Document(
        source_type=source_type,
        title=f"Test document {name}",
        content=f"Body of {name}.",
        service=service,
        external_id=f"TEST-{name}",
        path=f"{PREFIX}{name}.md",
        metadata={"severity": "high", "duration_minutes": 47, **MARKER},
    )


def _chunks(document: Document, count: int = 2) -> list[Chunk]:
    return [
        Chunk(
            content=f"{document.title} chunk {i}",
            chunk_index=i,
            token_count=10,
            section=f"Section {i}",
            metadata=document.chunk_metadata(),
        )
        for i in range(count)
    ]


def _filters(raw: dict | None = None) -> dict:
    """A filter over the test's own documents only."""
    return normalize({**MARKER, **(raw or {})})


async def _reachable(store: PgVectorStore) -> bool:
    try:
        await store.stats()
    except (SQLAlchemyError, OSError):
        return False

    return True


async def _remove_test_documents(store: PgVectorStore) -> None:
    async with store._engine.begin() as connection:  # noqa: SLF001 - test isolation
        await connection.execute(delete(documents).where(documents.c.path.like(f"{PREFIX}%")))


@pytest.fixture
async def store():  # noqa: ANN201
    """One store per test.

    Per test rather than per module because an asyncpg connection belongs to the event loop it
    was opened on, and pytest-asyncio gives each test its own. A module-scoped pool is reused
    across loops and fails on the second test with an error that names neither.
    """
    store = PgVectorStore.from_url(settings().database_url)

    if not await _reachable(store):
        await store.dispose()
        pytest.skip("PostgreSQL is not reachable; start it with `docker compose --profile core up`")

    await _remove_test_documents(store)

    yield store

    await _remove_test_documents(store)
    await store.dispose()


# ------------------------------------------------------------------- writing ----


async def test_a_document_and_its_chunks_round_trip(store) -> None:  # noqa: ANN001
    document = _document("pool", "orders")
    chunks = _chunks(document)

    outcome = await store.upsert(document, chunks, [_vector(i) for i in range(len(chunks))])

    assert outcome.chunks_written == 2
    assert outcome.replaced is False

    stored = [c for c in await store.all_chunks() if c.path == document.path]

    assert len(stored) == 2
    assert stored[0].title == "Test document pool"
    assert stored[0].service == "orders"
    assert stored[0].source_type is SourceType.RUNBOOK
    assert stored[0].section == "Section 0"


async def test_re_ingesting_replaces_rather_than_duplicates(store) -> None:  # noqa: ANN001
    document = _document("pool", "orders")

    await store.upsert(document, _chunks(document, 3), [_vector(i) for i in range(3)])
    outcome = await store.upsert(document, _chunks(document, 1), [_vector(9)])

    assert outcome.replaced is True

    stored = [c for c in await store.all_chunks() if c.path == document.path]

    # The old chunks went with the old document row. An orphan here would be a retrievable
    # result whose source no longer exists.
    assert len(stored) == 1


async def test_deleting_a_document_takes_its_chunks(store) -> None:  # noqa: ANN001
    document = _document("pool", "orders")
    await store.upsert(document, _chunks(document), [_vector(0), _vector(1)])

    async with store._engine.begin() as connection:  # noqa: SLF001 - exercising the cascade
        document_id = await connection.scalar(
            select(documents.c.id).where(documents.c.path == document.path)
        )
        await connection.execute(delete(documents).where(documents.c.id == document_id))
        remaining = await connection.scalar(
            select(document_chunks.c.id).where(document_chunks.c.document_id == document_id)
        )

    assert remaining is None


async def test_a_wrong_width_vector_is_refused(store) -> None:  # noqa: ANN001
    # Caught by the column rather than found later as a ranking that makes no sense.
    document = _document("pool", "orders")

    with pytest.raises(DBAPIError):
        await store.upsert(document, _chunks(document, 1), [[0.1, 0.2, 0.3]])


async def test_a_chunk_count_mismatch_is_caught_before_the_database(store) -> None:  # noqa: ANN001
    document = _document("pool", "orders")

    with pytest.raises(ValueError, match="2 chunks and 1 embeddings"):
        await store.upsert(document, _chunks(document, 2), [_vector(0)])


# ----------------------------------------------------------------- searching ----


async def test_search_returns_the_nearest_chunk_first(store) -> None:  # noqa: ANN001
    document = _document("pool", "orders")
    await store.upsert(document, _chunks(document, 2), [_vector(0), _vector(500)])

    hits = await store.search(_vector(500), k=5)
    ours = [h for h in hits if h.chunk.path == document.path]

    assert ours[0].chunk.chunk_index == 1
    # Reported as similarity rather than distance, so it runs the same direction as every other
    # score in the system.
    assert ours[0].score == pytest.approx(1.0, abs=1e-6)
    assert ours[0].rank >= 1


async def test_search_respects_a_metadata_filter(store) -> None:  # noqa: ANN001
    orders = _document("orders-doc", "orders")
    payments = _document("payments-doc", "payments")

    await store.upsert(orders, _chunks(orders, 1), [_vector(1)])
    await store.upsert(payments, _chunks(payments, 1), [_vector(1)])

    hits = await store.search(_vector(1), k=20, filters=_filters({"service": "payments"}))

    assert [h.chunk.service for h in hits] == ["payments"]


async def test_an_any_of_filter_matches_either_value(store) -> None:  # noqa: ANN001
    orders = _document("orders-doc", "orders")
    payments = _document("payments-doc", "payments")
    users = _document("users-doc", "users")

    for index, document in enumerate((orders, payments, users)):
        await store.upsert(document, _chunks(document, 1), [_vector(index + 1)])

    hits = await store.search(
        _vector(1), k=20, filters=_filters({"service": ["orders", "payments"]})
    )

    assert {h.chunk.service for h in hits} == {"orders", "payments"}


@pytest.mark.parametrize(
    "raw_filter",
    [
        {"service": "orders"},
        {"service": "payments"},
        {"service": ["orders", "users"]},
        {"document_type": "runbook"},
        {"document_type": "postmortem"},
        {"document_type": "runbook", "service": "orders"},
        {"severity": "high"},
        {"severity": "low"},
        # Numbers arrive as text over HTTP; both sides have to read them the same way.
        {"duration_minutes": 47},
        {"duration_minutes": "47"},
        # A key no chunk carries, which must exclude everything rather than be ignored.
        {"incident_id": "INC-00001"},
    ],
)
async def test_the_sql_filter_agrees_with_the_python_one(store, raw_filter) -> None:  # noqa: ANN001
    """The test this file exists for.

    ``rag.filters.matches`` decides what the lexical half returns and ``metadata_condition``
    decides what the dense half returns. If they disagree, a hybrid search silently drops
    results from one side and nothing anywhere reports an error.
    """
    corpus = [
        _document("orders-doc", "orders", SourceType.RUNBOOK),
        _document("payments-doc", "payments", SourceType.POSTMORTEM),
        _document("users-doc", "users", SourceType.RUNBOOK),
    ]

    for index, document in enumerate(corpus):
        await store.upsert(document, _chunks(document, 1), [_vector(index + 1)])

    filters = _filters(raw_filter)

    from_sql = {h.chunk.path for h in await store.search(_vector(1), k=50, filters=filters)}
    from_python = {c.path for c in await store.all_chunks() if matches(c.metadata, filters)}

    assert from_sql == from_python


# ------------------------------------------------------------------- ingest ----


async def test_content_hashes_are_keyed_by_path(store) -> None:  # noqa: ANN001
    document = _document("pool", "orders")
    await store.upsert(document, _chunks(document, 1), [_vector(0)])

    hashes = await store.content_hashes()

    assert hashes[document.path] == document.content_hash


async def test_delete_missing_removes_only_what_is_gone(store) -> None:  # noqa: ANN001
    kept = _document("kept", "orders")
    gone = _document("gone", "orders")

    await store.upsert(kept, _chunks(kept, 1), [_vector(0)])
    await store.upsert(gone, _chunks(gone, 1), [_vector(1)])

    # Everything currently indexed except the one document that should go, so the real corpus in
    # this database is untouched.
    keys = set(await store.content_hashes()) - {gone.path}
    removed = await store.delete_missing(keys)

    remaining = {c.path for c in await store.all_chunks() if str(c.path or "").startswith(PREFIX)}

    assert removed == 1
    assert remaining == {kept.path}


# ------------------------------------------------------------------ logging ----


async def test_a_retrieval_is_logged_with_both_candidate_lists(store) -> None:  # noqa: ANN001
    await store.log_retrieval(
        query="connection pool exhausted",
        retriever="hybrid",
        filters={"service": "orders"},
        candidates={"bm25": ["a"], "vector": ["b"], "fused": ["a", "b"]},
        latency_ms={"total": 41},
    )

    from rag.schema import retrieval_logs

    async with store._engine.connect() as connection:  # noqa: SLF001 - reading what we wrote
        row = (
            await connection.execute(
                select(retrieval_logs)
                .where(retrieval_logs.c.query == "connection pool exhausted")
                .order_by(retrieval_logs.c.created_at.desc())
                .limit(1)
            )
        ).one()

    assert row.retriever == "hybrid"
    assert row.bm25_ids == ["a"]
    assert row.vector_ids == ["b"]
    assert row.fused_ids == ["a", "b"]

    async with store._engine.begin() as connection:  # noqa: SLF001 - test teardown
        await connection.execute(
            delete(retrieval_logs).where(retrieval_logs.c.query == "connection pool exhausted")
        )


async def test_a_logging_failure_does_not_fail_the_search(store) -> None:  # noqa: ANN001
    # A search that answered is not failed because its audit row could not be written.
    await store.log_retrieval(
        query="bad investigation id",
        retriever="hybrid",
        filters={},
        candidates={},
        latency_ms={},
        investigation_id="not-a-uuid",
    )
