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

## `reasoning/*.json`

Five incidents' worth of evidence, one file each, for the agent-model benchmark
(`ai-service/evaluation/reasoning_eval.py`). One per implemented chaos scenario — 1 to 5 in
`sample-services/chaos/scenarios.md` — each holding the question, the service, the scenario code
the agent is expected to reach, and six or seven evidence items in the exact shape
`agents/nodes/collectors.py` produces them:

```json
{"id": "R03", "scenario": "DB_DEADLOCK", "expected_category": "DB_DEADLOCK",
 "incident_code": "INC-00144", "service": "payments",
 "query": "payments keeps failing and then recovering, what is causing it",
 "discriminator": "Errors are intermittent and self-recovering, and the database reports blocked/blocking pairs.",
 "evidence": [{"source": "database", "tool": "database-mcp/get_locks_and_deadlocks", "weight": 0.8,
               "summary": "deadlocks since reset: 14; sessions blocked now: 2 — ...", "raw": {}}]}
```

**The evidence is frozen on purpose, and that is what separates this from the Phase 11 agent
benchmark.** `agent_eval.py` enables a real scenario and scores the whole system, so its numbers
move when a tool, the load generator or Loki changes. These cases hold the facts still and vary
only the model, which is what makes "3B or 7B" a question with an answer: a model that concludes
wrongly here did so from evidence that contained the right answer.

Each case carries the scenario's own *discriminator* — the signal that separates it from the
scenario it is most easily confused with — and the evidence is written so that discriminator is
present. R02 has no errors at all and one statement dominating total time; R04 has no errors and
dozens of fast queries in one trace. A case whose evidence did not contain its discriminator
would be measuring whether the model guesses well, which is the opposite of the thing being
built.

Every case also carries evidence that is true and unhelpful — a healthy connection count, zero
deadlocks, unchanged latency — because a real collector run returns those, and a set of facts
that all point one way measures nothing about a model's ability to weigh them.

`weight` follows the collectors' three values: 0.8 looked and found something, 0.4 looked and
found nothing, 0.6 a gauge, with 0.9 for a saturation the server itself measured and 0.5 for a
retrieved document.

**Where the numbers stand** (2026-09-11, after the critic rebuild):

| model | correct root cause | finished | reached it | calls | mean |
|---|---|---|---|---|---|
| `qwen2.5:3b-instruct` | 3/5 | 2/5 | 3/5 | 4.0 | 74 s |
| `qwen2.5:7b-instruct` | 4/5 | 4/5 | 5/5 | 4.4 | 90 s |

The 3B's first two columns were 3 and 0 until the critic was rebuilt; what changed and what it
cost is [ADR-0007](../../docs/adr/0007-critic-veto-needs-grounds.md). The 7B writes the correct
conclusion in all five and loses one of them at the confidence threshold — R04, the n+1 query,
which neither model gets.

Two runs of the same set on the 3B are identical — temperature is 0 and decoding is constrained —
so a difference in this table is a change in the system rather than in the sampler. The 7B
repeated its five conclusions and its five confidences exactly with an embedding model loaded
first, at 140 s rather than 90 s: on 6 GB of VRAM Ollama evicts bge-m3 and keeps 82% of the 7B on
the GPU either way.

## `runs/*`

What `python -m evaluation.suite` wrote: one directory per invocation, holding `suite.json` (the
record the Evaluation page draws) and one file per benchmark with its full output. Committed
rather than ignored, for the same reason `datasets/routing/benchmark.json` is — a fresh checkout
should show real numbers, and a chart with nothing behind it teaches nobody what the system is
worth.

## The same fixtures, two different benchmarks

`reasoning/*.json` is read by **two** evaluators, and the difference between them is the whole
reason both exist.

`reasoning_eval` replays the recorded evidence and varies only the model. Nothing else moves, so
"3B or 7B" has an answer.

`agent_eval` ignores the recorded evidence and uses the same files for their *questions*: it
enables the scenario on the running service, drives load, and lets the collectors gather whatever
they actually gather. Its numbers move when a tool changes, when Loki is slow, when the router
plans different collectors. When the two disagree, that one is measuring the model and this one
is measuring the system.

### What breaking things for real taught, on the first runs

**Enabling a scenario is not breaking something.** Driving 2366 requests at 24 concurrent left
the pool busy rather than exhausted: every request succeeded, the telemetry showed a slow but
healthy service, and the agent correctly investigated an incident that was not happening.
`agent_eval` now checks the load against the baseline and records a case whose service kept
serving as *not run*, with its numbers — scoring it as a wrong answer would measure nothing.

**The load has to reach the path the fault lives on.** 6413 checkouts through the gateway at 150
concurrent failed none, because the pool scenario makes `GET /orders` hold a connection. The load
goes to the owning service's own endpoint now.

**Not every fault fails requests.** The missing-index scenario served everything and served it at
a fifth of the baseline rate — 18/s against 100/s, zero errors. A reproduction check that counted
only failures called that "did not reproduce". Throughput collapse counts too.

**Two of the five scenarios need load nobody drives yet.** `DB_DEADLOCK` wants concurrent writes
to payments and `NULL_REFERENCE_EXCEPTION` wants one particular currency; a generic read against
a list endpoint leaves both untouched. They are reported as scenarios that did not reproduce
rather than as failures of the agent, and per-scenario load recipes are the fix.
