"""The benchmark runner, and the fixture it runs on.

Two different things are covered here. The first is the loader, whose whole job is to refuse a
query set that would produce a plausible wrong number — every validation it does corresponds to
a mistake that lowers a score silently rather than raising.

The second is the fixture itself. `test_every_labelled_document_exists` reads
`datasets/evaluation/rag_queries.jsonl` against `datasets/knowledge/` and fails if a labelled
path is not a file. That is a test of data rather than of code, and it belongs in the suite
because renaming a runbook is a normal thing to do and turns four queries into guaranteed misses
that look like a retrieval regression.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.postmortem import GENERATED_DIR
from evaluation.rag_eval import (
    DEFAULT_QUERIES,
    EvalError,
    check_labels,
    document_keys,
    load_queries,
)
from rag.documents import RetrievedChunk, SourceType, StoredChunk

KNOWLEDGE_BASE = DEFAULT_QUERIES.parent.parent / "knowledge"


def _write(tmp_path: Path, *rows: dict) -> Path:
    path = tmp_path / "queries.jsonl"
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    return path


def _valid(query_id: str = "Q001") -> dict:
    return {
        "id": query_id,
        "query": "connection pool exhausted",
        "relevant_documents": ["runbooks/connection-pool-exhaustion.md"],
        "kind": "error_string",
    }


# --------------------------------------------------------------------- loading ----


def test_a_query_set_loads_with_its_filters(tmp_path: Path) -> None:
    row = _valid() | {"filters": {"service": "orders"}}

    queries = load_queries(_write(tmp_path, row))

    assert queries[0].id == "Q001"
    assert queries[0].filters == {"service": "orders"}
    assert queries[0].kind == "error_string"


def test_a_duplicate_id_is_refused(tmp_path: Path) -> None:
    # It would double-weight one query in every average, and nothing about the output would say
    # so.
    with pytest.raises(EvalError, match="duplicate"):
        load_queries(_write(tmp_path, _valid(), _valid()))


def test_a_query_with_no_labelled_document_is_refused(tmp_path: Path) -> None:
    """The one that would be invisible.

    By the definition in metrics.recall_at_k a query wanting nothing scores 1.0 for every
    retriever, so an unlabelled query silently pulls every row of the table up.
    """
    row = _valid() | {"relevant_documents": []}

    with pytest.raises(EvalError, match="labels no relevant document"):
        load_queries(_write(tmp_path, row))


def test_an_empty_query_is_refused(tmp_path: Path) -> None:
    row = _valid() | {"query": "   "}

    with pytest.raises(EvalError, match="empty query"):
        load_queries(_write(tmp_path, row))


def test_malformed_json_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / "queries.jsonl"
    path.write_text(json.dumps(_valid()) + "\n{not json}\n", encoding="utf-8")

    with pytest.raises(EvalError, match="line 2"):
        load_queries(path)


def test_blank_lines_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "queries.jsonl"
    path.write_text(json.dumps(_valid()) + "\n\n", encoding="utf-8")

    assert len(load_queries(path)) == 1


def test_a_missing_file_says_so(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="not found"):
        load_queries(tmp_path / "nothing.jsonl")


# ------------------------------------------------------------- label checking ----


def test_a_label_the_index_does_not_have_stops_the_run() -> None:
    """Otherwise the run still produces a table, with every retriever mysteriously worse.

    A renamed file or a corpus ingested from somewhere else turns every query labelled with it
    into a guaranteed miss, and nothing distinguishes that from a retrieval regression.
    """
    queries = load_queries(DEFAULT_QUERIES)

    with pytest.raises(EvalError, match="not in the index"):
        check_labels(queries, indexed={"runbooks/connection-pool-exhaustion.md"})


# -------------------------------------------------------- chunks to documents ----


def _hit(chunk_id: str, path: str, rank: int) -> RetrievedChunk:
    stored = StoredChunk(
        chunk_id=chunk_id,
        document_id=f"doc-{path}",
        content="content",
        chunk_index=0,
        title=path,
        source_type=SourceType.RUNBOOK,
        path=path,
    )

    return RetrievedChunk(chunk=stored, score=0.5, rank=rank, retriever="hybrid")


def test_five_chunks_can_be_three_documents() -> None:
    hits = [
        _hit("c1", "runbooks/pool.md", 1),
        _hit("c2", "runbooks/pool.md", 2),
        _hit("c3", "incidents/INC-00001.md", 3),
    ]

    assert document_keys(hits) == ["runbooks/pool.md", "incidents/INC-00001.md"]


def test_a_document_is_scored_where_it_first_appears() -> None:
    # Recall@1 asks what the top chunk cited, not what the top document was after deduplication.
    hits = [_hit("c1", "services/users.md", 1), _hit("c2", "runbooks/pool.md", 2)]

    assert document_keys(hits)[0] == "services/users.md"


# ------------------------------------------------------------------- fixture ----


def test_the_shipped_query_set_is_valid() -> None:
    queries = load_queries(DEFAULT_QUERIES)

    assert len(queries) >= 50
    assert {q.language for q in queries} >= {"en", "tr"}


def test_every_labelled_document_exists() -> None:
    """Ground truth that points at a file nobody has is not ground truth."""
    queries = load_queries(DEFAULT_QUERIES)
    missing = sorted(
        {
            key
            for query in queries
            for key in query.relevant_documents
            if not (KNOWLEDGE_BASE / key).is_file()
        }
    )

    assert missing == []


def test_the_query_set_covers_every_document_in_the_corpus() -> None:
    """A corpus with documents no query asks for is a benchmark with blind spots.

    Not a strict requirement of the metrics — it is a requirement of the query set being an
    honest sample of what the agent will ask, and it is how the last two service docs got
    queries at all.

    Postmortems the agent wrote are exempt, and have to be: Phase 9 has a concluded investigation
    write one back into the corpus, so the directory holds whatever the last demo run concluded.
    A query set cannot label a document that does not exist until someone runs the system, and
    without the exemption the suite passes in CI — where the directory is empty — and fails on
    any machine the demo has been run on, which is the wrong way round.
    """
    labelled = {
        key for query in load_queries(DEFAULT_QUERIES) for key in query.relevant_documents
    }
    corpus = {
        path.relative_to(KNOWLEDGE_BASE).as_posix()
        for path in KNOWLEDGE_BASE.rglob("*.md")
        if not path.name.startswith("_") and path.parent != KNOWLEDGE_BASE / GENERATED_DIR
    }

    assert corpus - labelled == set()


def test_filters_only_use_keys_retrieval_knows_how_to_apply() -> None:
    from rag.filters import KNOWN_KEYS

    for query in load_queries(DEFAULT_QUERIES):
        assert set(query.filters) <= set(KNOWN_KEYS), query.id
