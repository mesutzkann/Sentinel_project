"""BM25 and the tokenizer it runs on.

The tokenizer gets as much attention as the ranking because it is where lexical retrieval is
actually won or lost on this corpus: the queries are about `MaxPoolSize`, `40P01` and
`PaymentProcessor.cs`, and a tokenizer that throws those away leaves BM25 with nothing to match
that the embedding could not already find.
"""

from __future__ import annotations

from rag.documents import SourceType, StoredChunk
from rag.lexical import Bm25Index, tokenize


def _chunk(chunk_id: str, content: str, **metadata: str) -> StoredChunk:
    return StoredChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{chunk_id}",
        content=content,
        chunk_index=0,
        title=f"Document {chunk_id}",
        source_type=SourceType.RUNBOOK,
        service=metadata.get("service"),
        metadata={"document_type": "runbook", **metadata},
    )


POOL = _chunk(
    "pool",
    "Connection pool exhaustion. Npgsql.NpgsqlException: the connection pool has been "
    "exhausted. Raise MaxPoolSize back to 200.",
    service="orders",
)
DEADLOCK = _chunk(
    "deadlock",
    "Deadlock detected. PostgreSQL raises 40P01 and kills one transaction. Fix the lock "
    "ordering in PaymentProcessor.cs.",
    service="payments",
)
MEMORY = _chunk(
    "memory",
    "Memory leak. The working set climbs monotonically and gen-2 collections climb with it.",
    service="notifications",
)

INDEX = Bm25Index()
INDEX.build([POOL, DEADLOCK, MEMORY])


def test_an_identifier_is_indexed_whole_and_in_parts() -> None:
    terms = tokenize("MaxPoolSize")

    assert "maxpoolsize" in terms
    assert {"max", "pool", "size"} <= set(terms)


def test_a_dotted_identifier_keeps_its_parts() -> None:
    terms = tokenize("Npgsql.NpgsqlException")

    assert "npgsql.npgsqlexception" in terms
    assert "npgsqlexception" in terms


def test_a_plain_word_is_not_indexed_twice() -> None:
    # Splitting `orders` yields `orders` again, and a duplicate inflates its term frequency.
    assert tokenize("orders").count("orders") == 1


def test_turkish_casing_folds_to_the_same_token() -> None:
    # `İ`.lower() is `i` plus a combining dot in Python, which would not match a plain `i`.
    assert tokenize("İSTEK") == tokenize("istek")


def test_a_query_written_as_words_finds_the_identifier() -> None:
    hits = INDEX.search("max pool size", k=3)

    assert hits[0].chunk_id == "pool"


def test_an_error_code_matches_exactly() -> None:
    hits = INDEX.search("40P01", k=3)

    assert [h.chunk_id for h in hits] == ["deadlock"]


def test_a_file_name_matches() -> None:
    assert INDEX.search("PaymentProcessor.cs", k=3)[0].chunk_id == "deadlock"


def test_ranks_start_at_one_and_are_consecutive() -> None:
    hits = INDEX.search("connection pool deadlock memory", k=3)

    assert [h.rank for h in hits] == [1, 2, 3]


def test_a_query_with_no_matching_term_returns_nothing() -> None:
    assert INDEX.search("kubernetes ingress certificate", k=5) == []


def test_an_empty_index_returns_nothing_rather_than_failing() -> None:
    assert Bm25Index().search("anything", k=5) == []


def test_a_term_in_every_chunk_never_lowers_a_score() -> None:
    """The reason the IDF is Lucene's rather than the textbook Okapi one.

    With ``log((N - df + 0.5) / (df + 0.5))`` a term present in every chunk scores negative, so
    a chunk can be pushed *down* for containing a query term. Over a corpus about five services,
    "the" and "service" are that term.
    """
    index = Bm25Index()
    index.build([
        _chunk("a", "the orders service is slow"),
        _chunk("b", "the payments service is slow"),
        _chunk("c", "the users service is slow"),
    ])

    with_common_term = index.search("the service orders", k=3)
    without = index.search("orders", k=3)

    assert with_common_term[0].chunk_id == "a"
    assert with_common_term[0].score >= without[0].score


def test_a_metadata_filter_excludes_non_matching_chunks() -> None:
    hits = INDEX.search("connection pool deadlock memory", k=5, filters={"service": "payments"})

    assert [h.chunk_id for h in hits] == ["deadlock"]


def test_an_any_of_filter_matches_either_value() -> None:
    hits = INDEX.search(
        "connection pool deadlock memory", k=5, filters={"service": ["payments", "orders"]}
    )

    assert {h.chunk_id for h in hits} == {"deadlock", "pool"}


def test_a_filter_on_a_key_no_chunk_carries_returns_nothing() -> None:
    assert INDEX.search("pool", k=5, filters={"incident_id": "INC-00001"}) == []


def test_rebuilding_replaces_the_corpus_rather_than_adding_to_it() -> None:
    index = Bm25Index()
    index.build([POOL, DEADLOCK])
    index.build([MEMORY])

    assert index.size == 1
    assert index.search("connection pool", k=5) == []
