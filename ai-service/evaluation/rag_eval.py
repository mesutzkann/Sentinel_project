"""The retrieval benchmark: four retrievers, one query set, one table.

    python -m evaluation.rag_eval                    # from ai-service/, against an ingested corpus
    python -m evaluation.rag_eval --output run.json  # keep the per-query detail

It needs the same two things a search needs — PostgreSQL with the `rag` schema ingested, and
Ollama with bge-m3 — plus the cross-encoder for the fourth row. It refuses to run rather than
reporting a partial comparison: a table whose `hybrid_rerank` row is silently the fused numbers
would be the most misleading artefact this project could produce.

Two design decisions are load-bearing.

*The reranker does not degrade here.* The API's `hybrid_rerank` falls back to the fused order
when the cross-encoder is missing, because a user waiting on a search wants results. A benchmark
wants the truth, so this builds its own retriever with `degrade=False` and reports the row as
unavailable instead.

*Nothing is written to `retrieval_logs`.* An evaluation is 240 searches, and filling the table
the agent's searches are diagnosed from with benchmark traffic would make it useless for the
thing it exists for.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings, settings
from evaluation.metrics import (
    QueryOutcome,
    RetrieverScores,
    comparison_table,
    recall_at_k,
    summarize,
)
from rag.documents import RetrievedChunk
from rag.embeddings import OllamaEmbeddingProvider
from rag.lexical import Bm25Index
from rag.rerank import CrossEncoderReranker
from rag.retrievers import (
    Bm25Retriever,
    HybridRerankRetriever,
    HybridRetriever,
    Retriever,
    VectorRetriever,
)
from rag.store import PgVectorStore

# ai-service/evaluation/rag_eval.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUERIES = _REPO_ROOT / "datasets" / "evaluation" / "rag_queries.jsonl"


class EvalError(RuntimeError):
    """The benchmark cannot run, or cannot run honestly."""


@dataclass(frozen=True, slots=True)
class EvalQuery:
    """One labelled query from the fixture file."""

    id: str
    query: str
    relevant_documents: list[str]
    kind: str = "unspecified"
    language: str = "en"
    filters: dict[str, Any] = field(default_factory=dict)


def load_queries(path: Path) -> list[EvalQuery]:
    """Read and validate the fixture.

    Validation is strict and the errors name the line, because every one of these mistakes
    lowers a score rather than raising anything: a duplicated id silently double-weights a
    query, and a query with no labelled document scores every retriever 1.0 by the definition in
    ``metrics.recall_at_k`` and drags the whole average up.
    """
    if not path.is_file():
        raise EvalError(f"Query set not found: {path}")

    queries: list[EvalQuery] = []
    seen: set[str] = set()

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue

        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalError(f"{path.name} line {number} is not valid JSON: {exc}") from exc

        query_id = str(row.get("id", "")).strip()

        if not query_id:
            raise EvalError(f"{path.name} line {number} has no id.")

        if query_id in seen:
            raise EvalError(f"{path.name} line {number}: duplicate query id {query_id}.")

        if not row.get("query", "").strip():
            raise EvalError(f"{path.name} line {number} ({query_id}) has an empty query.")

        relevant = row.get("relevant_documents") or []

        if not relevant:
            raise EvalError(f"{path.name} line {number} ({query_id}) labels no relevant document.")

        seen.add(query_id)
        queries.append(
            EvalQuery(
                id=query_id,
                query=row["query"],
                relevant_documents=list(relevant),
                kind=row.get("kind", "unspecified"),
                language=row.get("language", "en"),
                filters=row.get("filters") or {},
            )
        )

    if not queries:
        raise EvalError(f"{path} has no queries.")

    return queries


def document_keys(chunks: list[RetrievedChunk]) -> list[str]:
    """The documents behind a ranking, in rank order, without repeats.

    Five chunks can be three documents; the metrics are defined over documents, and the position
    that counts is where the document *first* appears.
    """
    keys: list[str] = []

    for chunk in chunks:
        key = chunk.chunk.path or chunk.chunk.external_id

        if key and key not in keys:
            keys.append(key)

    return keys


def check_labels(queries: list[EvalQuery], indexed: set[str]) -> None:
    """Fail if the fixture names a document the index does not have.

    This is the failure that would otherwise be invisible. A renamed file, or a corpus ingested
    from a different directory, turns every query labelled with it into a guaranteed miss — and
    the run still produces a table, with every retriever equally and mysteriously worse.
    """
    labelled = {key for query in queries for key in query.relevant_documents}
    missing = sorted(labelled - indexed)

    if missing:
        raise EvalError(
            "The query set labels documents that are not in the index:\n  "
            + "\n  ".join(missing)
            + "\n\nRun POST /rag/ingest, or check the paths in the fixture."
        )


async def build_retrievers(
    config: Settings,
    store: PgVectorStore,
    include_rerank: bool,
) -> tuple[dict[str, Retriever], set[str], str | None]:
    """Assemble all four retrievers over the ingested corpus.

    Returns them alongside the set of document keys in the index (for label checking) and the
    reason the reranker is unavailable, if it is.
    """
    chunks = await store.all_chunks()

    if not chunks:
        raise EvalError(
            "The rag schema has no chunks. Ingest the knowledge base first: "
            "curl -X POST http://localhost:8000/rag/ingest -d '{}'"
        )

    lexical = Bm25Index()
    lexical.build(chunks)

    embeddings = OllamaEmbeddingProvider(
        base_url=config.ollama_base_url,
        model=config.embedding_model,
        dimensions=config.embedding_dimensions,
    )

    if not await embeddings.is_available():
        raise EvalError(
            f"Ollama at {config.ollama_base_url} does not have '{config.embedding_model}'. "
            "Three of the four retrievers need it, so there is nothing to compare."
        )

    bm25 = Bm25Retriever(lexical)
    vector = VectorRetriever(embeddings, store)
    hybrid = HybridRetriever(bm25, vector, candidates=config.retrieval_candidates)

    retrievers: dict[str, Retriever] = {"bm25": bm25, "vector": vector, "hybrid": hybrid}
    unavailable: str | None = None

    if include_rerank:
        reranker = CrossEncoderReranker(
            model=config.rerank_model,
            device=config.rerank_device_or_auto,
            max_length=config.rerank_max_length,
            batch_size=config.rerank_batch_size,
        )

        if await reranker.is_available():
            retrievers["hybrid_rerank"] = HybridRerankRetriever(
                hybrid, reranker, candidates=config.retrieval_candidates, degrade=False
            )
        else:
            unavailable = reranker.unavailable_reason or "the cross-encoder could not be loaded"

    indexed = {chunk.path or chunk.external_id or "" for chunk in chunks}

    return retrievers, indexed, unavailable


async def run(
    retrievers: dict[str, Retriever],
    queries: list[EvalQuery],
    k: int,
) -> dict[str, list[QueryOutcome]]:
    """Every query through every retriever, sequentially.

    Sequentially on purpose. The latency columns are part of the comparison, and running four
    retrievers concurrently against one Ollama and one CPU would measure the contention rather
    than the retrievers.
    """
    outcomes: dict[str, list[QueryOutcome]] = {}

    for name, retriever in retrievers.items():
        collected: list[QueryOutcome] = []

        for query in queries:
            started = time.perf_counter()
            result = await retriever.retrieve(query.query, k, query.filters or None)
            elapsed = int((time.perf_counter() - started) * 1000)

            collected.append(
                QueryOutcome(
                    query_id=query.id,
                    query=query.query,
                    retriever=name,
                    retrieved=document_keys(result.chunks),
                    relevant=query.relevant_documents,
                    latency_ms=elapsed,
                )
            )

        outcomes[name] = collected
        print(f"  {name}: {len(collected)} queries", file=sys.stderr)

    return outcomes


def recall_by_kind(
    outcomes: dict[str, list[QueryOutcome]],
    queries: list[EvalQuery],
    k: int = 5,
) -> str:
    """Recall@k split by what the query is testing.

    The single most informative table this benchmark produces, and the one that shows why the
    hybrid exists rather than asserting it: BM25 and the dense retriever each own a column, and
    neither owns both.
    """
    kinds = sorted({query.kind for query in queries})
    kind_of = {query.id: query.kind for query in queries}

    header = f"| retriever | {' | '.join(kinds)} |\n|---|{'---|' * len(kinds)}"
    rows = []

    for name, collected in outcomes.items():
        grouped: dict[str, list[float]] = defaultdict(list)

        for outcome in collected:
            grouped[kind_of[outcome.query_id]].append(
                recall_at_k(outcome.retrieved, outcome.relevant, k)
            )

        cells = [
            f"{sum(grouped[kind]) / len(grouped[kind]):.3f}" if grouped[kind] else "—"
            for kind in kinds
        ]
        rows.append(f"| {name} | {' | '.join(cells)} |")

    return "\n".join([header, *rows])


def report(
    outcomes: dict[str, list[QueryOutcome]],
    queries: list[EvalQuery],
    k: int,
) -> tuple[str, list[RetrieverScores]]:
    """The text a person reads, and the scores a caller keeps."""
    scores = [summarize(collected) for collected in outcomes.values()]
    by_id = {query.id: query for query in queries}

    sections = [
        f"# Retrieval evaluation — {len(queries)} queries, k={k}",
        "",
        comparison_table(scores),
        "",
        f"## Recall@{k} by query kind",
        "",
        recall_by_kind(outcomes, queries, k),
    ]

    for score in sorted(scores, key=lambda s: -s.recall_at_5):
        if not score.misses:
            continue

        sections += ["", f"## What {score.retriever} could not find", ""]
        sections += [f"- `{qid}` {by_id[qid].query}" for qid in score.misses]

    return "\n".join(sections), scores


def as_json(
    outcomes: dict[str, list[QueryOutcome]],
    scores: list[RetrieverScores],
    queries: list[EvalQuery],
    k: int,
) -> dict[str, Any]:
    """The whole run, including per-query detail.

    Written out so that two runs can be diffed. An average that moved is a question; the query
    that stopped working is the answer, and it is not recoverable from the average.
    """
    return {
        "k": k,
        "queries": len(queries),
        "scores": [
            {
                "retriever": s.retriever,
                "recall_at_1": round(s.recall_at_1, 4),
                "recall_at_3": round(s.recall_at_3, 4),
                "recall_at_5": round(s.recall_at_5, 4),
                "mrr": round(s.mrr, 4),
                "precision_at_5": round(s.precision_at_5, 4),
                "latency_p50_ms": s.latency_p50_ms,
                "latency_p95_ms": s.latency_p95_ms,
                "misses": s.misses,
            }
            for s in scores
        ],
        "outcomes": {
            name: [
                {
                    "query_id": o.query_id,
                    "query": o.query,
                    "retrieved": o.retrieved,
                    "relevant": o.relevant,
                    "hit_rank": o.hit_rank,
                    "latency_ms": o.latency_ms,
                }
                for o in collected
            ]
            for name, collected in outcomes.items()
        },
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES, help="fixture path")
    parser.add_argument("--k", type=int, default=5, help="chunks retrieved per query")
    parser.add_argument("--output", type=Path, help="write the full run as JSON to this path")
    parser.add_argument(
        "--no-rerank",
        action="store_true",
        help="skip the cross-encoder row (the other three need no extra dependency)",
    )
    args = parser.parse_args(argv)

    config = settings()
    store = PgVectorStore.from_url(config.database_url)

    try:
        queries = load_queries(args.queries)
        retrievers, indexed, unavailable = await build_retrievers(
            config, store, include_rerank=not args.no_rerank
        )
        check_labels(queries, indexed)

        if unavailable:
            print(f"! hybrid_rerank skipped: {unavailable}", file=sys.stderr)

        print(
            f"Running {len(queries)} queries through {len(retrievers)} retrievers",
            file=sys.stderr,
        )
        outcomes = await run(retrievers, queries, args.k)
    except EvalError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    finally:
        await store.dispose()

    text, scores = report(outcomes, queries, args.k)
    print(text)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(as_json(outcomes, scores, queries, args.k), indent=2), encoding="utf-8"
        )
        print(f"\nWrote {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
