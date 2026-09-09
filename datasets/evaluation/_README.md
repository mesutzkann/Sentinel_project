# Evaluation fixtures

Ground truth for the benchmarks in `ai-service/evaluation/`. Nothing here is ingested into the
knowledge base — these are the questions asked of it, and the answers a human agreed to.

## `rag_queries.jsonl`

A hundred and twenty queries over the 28 documents in `datasets/knowledge/`, one JSON object
per line:

```json
{"id": "Q013", "query": "latency climbed to the client timeout and stayed there, but the database itself looks healthy",
 "language": "en", "kind": "symptom",
 "relevant_documents": ["runbooks/connection-pool-exhaustion.md", "incidents/INC-00001-orders-pool-exhaustion.md"]}
```

`relevant_documents` are paths relative to `datasets/knowledge/`, which is how the ingestion
pipeline keys a document. Chunk ids would have been the more precise label and the wrong one:
they are generated at ingest and change whenever the chunker's target size does, so a query set
written against them would go quietly wrong rather than obviously stale.

`filters` is optional and is passed to the retriever with the query. A filtered query measures
something the unfiltered ones cannot: that both halves of a hybrid search apply the filter
identically, since a filter honoured by one half and ignored by the other still returns results.

### How relevance was decided

A document is labelled relevant if a person answering that question would want to read it. Two
consequences worth stating, because they are the difference between a benchmark and a number:

- **Both the runbook and the postmortem, when both genuinely answer.** "The connection pool has
  been exhausted" is answered by the runbook (what to do) and by INC-00001 (what happened last
  time). Labelling only one would score a correct retrieval as a miss.
- **Not everything that mentions the words.** `services/orders.md` has a connection-pool section
  and is not labelled for Q001, because it documents the setting rather than the failure. A
  query set that labels every mention measures term overlap, which is the thing being tested.

### The five kinds, and what each is for

| `kind` | n | What it tests |
|---|---|---|
| `error_string` | 22 | Exact identifiers and stack traces. What BM25 should win — an embedding of `40P01` is not close to anything. |
| `symptom` | 30 | The failure described in an engineer's words, sharing few terms with the document. What the dense half is for. |
| `cross_lingual` | 26 | Turkish questions against an English corpus. The property bge-m3 was chosen for; BM25 scores near zero on these by construction. |
| `lookup` | 22 | Questions about the estate rather than about a failure — dependencies, endpoints, where the telemetry is. |
| `filtered` | 20 | Metadata filters as part of the query, including short queries that are underspecified without one. |

### Why it is 120 and not 60

The set was doubled after its first benchmark run, and the run is why. At 60 queries `lookup`
and `filtered` had eight each: one query moved either column by 0.125, which is larger than
most of the differences the table was being read for. Two of the conclusions drawn from that
run did not survive the larger set — the reranker looked worse than plain fusion at rank one
(0.817 against 0.867) and is now level with it (0.850), and `lookup` looked like a column BM25
won.

No document was added and none was dropped, so the corpus the numbers are over is the same one;
what changed is how many questions each column rests on. Nothing here is generated: every query
was written by hand against the document it is labelled with, and a query that reuses the
document's own sentence was rewritten, because a query set that quotes the corpus measures term
overlap rather than retrieval.

Run it with `python -m evaluation.rag_eval` from `ai-service/`, against an ingested knowledge
base. `--help` lists the options; `ai-service/evaluation/metrics.py` defines the metrics.
