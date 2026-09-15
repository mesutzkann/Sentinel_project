# SentinelAI

Autonomous incident investigation and resolution platform. Given an incident on a microservice,
it investigates the way an on-call engineer would — reading logs, metrics, traces, recent
commits and source code — proposes a root cause with a confidence score, and, once a human
approves, applies the fix and verifies that it worked.

Everything runs locally. No hosted model, no paid API.

> **Status: Phase 7 of 12.** Foundation, observability, the local LLM layer, the MCP tool
> surface, hybrid retrieval and its reranker — and the investigation agent, which uses all of
> them. `make demo` breaks a service for real and watches the agent work out what happened. See
> [docs/planning.md](docs/planning.md) for the full roadmap and
> [Phase status](#phase-status) for what works today.

## What makes it interesting

- **A visible agent state machine.** Written by hand rather than assembled from a framework
  ([ADR-0003](docs/adr/0003-custom-state-machine-over-langgraph.md)), so every transition is an
  event the UI can draw and a person can reason about.
- **A fine-tuned router.** A 1.5B model trained with QLoRA to turn a question into an intent and
  a tool plan in under 150ms, benchmarked against its own base model.
- **Four retrievers behind one interface.** BM25, dense, hybrid with reciprocal rank fusion, and
  hybrid plus cross-encoder reranking — measured against the same 120-query set rather than
  asserted. `POST /rag/search` takes the retriever by name and `python -m evaluation.rag_eval`
  prints the comparison, so it is something you run rather than something you are told.
- **Real failures, not fixtures.** Five sample microservices with
  [15 chaos scenarios](sample-services/chaos/scenarios.md). Enabling one genuinely breaks the
  service; the telemetry is a side effect, exactly as in production. Six of the fifteen produce
  no error logs at all from the failing component, so an agent that only reads logs cannot solve
  them.

## Architecture

```text
React + TypeScript frontend
        │ REST · SignalR
ASP.NET Core 8 backend  ──────────  PostgreSQL 17 + pgvector
        │ HTTP + callbacks                (sentinel · rag · svc_*)
Python FastAPI AI service
        │ MCP (Streamable HTTP)      │ HTTP
8 MCP servers, ~40 tools            Ollama (qwen2.5)
        │
Loki · Prometheus · Jaeger · Grafana
        ▲ OTLP
5 .NET sample microservices + chaos middleware
```

Design decisions and their reasoning live in [docs/adr/](docs/adr/).

## Requirements

| | Minimum | This project was developed on |
|---|---|---|
| RAM | 16 GB | 16 GB |
| GPU | none (CPU inference) | RTX 3060 Laptop, 6 GB VRAM |
| Disk | 40 GB free | — |

Tooling: .NET 8 SDK, Node 20+, Docker with a working WSL2 backend on Windows, Python 3.11 and
Ollama (from Phase 3).

Because 6 GB of VRAM does not comfortably hold a 7B model alongside the rest of the stack,
`qwen2.5:3b-instruct` is the development default and `qwen2.5:7b-instruct` is switched on for
demos and benchmark runs. The model name is configuration, not code.

## Getting started

```bash
cp .env.example .env

# PostgreSQL 17 with pgvector, pg_stat_statements and the six schemas.
docker compose --profile core up -d

# Backend: migrates, seeds an admin and the five services, then serves on :5080
dotnet run --project backend/src/Sentinel.Api

# Frontend on :5173
cd frontend && npm install && npm run dev
```

Sign in with the credentials from `Seed:AdminUsername` / `Seed:AdminPassword`
(`admin` / `Admin123!` by default). There is no signup endpoint by design.

To run the sample services as well:

```bash
docker compose --profile core --profile samples up -d --build
curl http://localhost:8080/api/services/health
```

## The demo

One command, about three and a half minutes, and nothing is simulated:

```bash
make demo                 # or: ./scripts/dev.ps1 demo
```

It resets the estate, drives healthy traffic, drops `orders`' connection pool from 200 to 20
while the load continues, raises an incident in the words a person would use — *"orders is timing
out, find out why"* — and hands it to the agent. **The scenario code appears nowhere in the
question, the hint or the evidence.** The only route to the answer is the telemetry the failure
produced.

A run on the machine this was built on, with the 3B:

```text
[3/6] Enabling DB_CONNECTION_POOL_EXHAUSTION on orders
      1560 served, 1716 failed (52%) - timeouts waiting for a connection

[5/6] Investigating
         3s  PLAN                  FULL_INVESTIGATION on orders: 6 collector(s)
        12s  COLLECT_DATABASE      COLLECT_DATABASE: 3 fact(s) from 3 call(s)
        18s  SEARCH_HISTORY        3 document(s) from the knowledge base via hybrid_rerank
        30s  RANK_HYPOTHESES       4 ranked; 'Connection Pool Exhaustion' leads at 0.95, by 0.02
        36s  VALIDATE              the critic accepts the conclusion; confidence 0.79
        43s  RECOMMEND_FIX         3 recommendation(s), all requiring approval

      13 facts over 7 sources, 10 tool calls, 4 model calls, 42s
      DB_CONNECTION_POOL_EXHAUSTION  (correct)  confidence 0.79
```

The load matters more than it looks. At 60 concurrent requests the shrunken pool is merely busy:
everything queues, everything succeeds, and the agent correctly investigates an incident that is
not happening. The demo drives 150, which is above the measured knee at ~120, and the pool
produces the timeouts the scenario is recognised by.

Watch it fill in at <http://localhost:5173/investigations>, or read the same run over HTTP at
`GET /api/investigations/{id}`.

## Observability

```bash
docker compose --profile core --profile samples --profile observability up -d --build
```

The five services export traces, metrics and logs over OTLP to one collector, which fans them
out to Jaeger, Prometheus and Loki. A sixth container, `delivery-provider`, deliberately exports
nothing: it stands in for a third party, and chaos scenario 12 is recognised by a failure that
terminates at an external span — which only holds while that process is invisible to Jaeger. Grafana arrives with all three wired up and provisioned
dashboards — nothing is clicked together by hand.

SentinelAI reports into the same three. The backend and the AI service export under the standard
HTTP semantic conventions, so they appear in `Service Health` next to the services they
investigate; on top of that the AI service records what only it has — investigations by outcome
and duration, where an investigation's minutes went by state, model call latency and tokens, MCP
tool calls by server and outcome, and knowledge base search latency. That is the `SentinelAI
Itself` dashboard. Both natively run processes read `OTEL_EXPORTER_OTLP_ENDPOINT` and stay silent
when it is unset, so neither needs the observability profile to start.

| | | |
|---|---|---|
| Grafana | <http://localhost:3000> | `Service Health` and `SentinelAI Itself`, under the SentinelAI folder |
| Jaeger | <http://localhost:16686> | one checkout is a five-service trace |
| Prometheus | <http://localhost:9090> | `job` is the service name, matching `services.metrics_job` |
| Loki | <http://localhost:3100> | queried by the dashboard, not usually directly |

Break something and watch it show up:

```bash
# A subset of requests starts failing, latency unaffected.
curl -X POST http://localhost:8083/chaos/NULL_REFERENCE_EXCEPTION/enable
curl -X POST http://localhost:8083/payments/authorize   -H 'Content-Type: application/json'   -d '{"order_id":"11111111-1111-1111-1111-111111111111","amount":10,"currency":"JPY"}'

curl -X POST http://localhost:8083/chaos/reset
```

Thirteen of the fifteen scenarios have their behaviour implemented
([the catalogue](sample-services/chaos/scenarios.md) marks which); the rest are declared and
answer `GET /chaos`, with behaviour landing in later phases.

## The AI service

Reasoning runs locally through Ollama. Nothing leaves the machine and nothing is billed.

```bash
ollama pull qwen2.5:3b-instruct        # ~2 GB; the development default

cd ai-service
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # Scripts/ -> bin/ on Linux
.venv/Scripts/uvicorn app.main:app --port 8000
```

`GET /llm/health` reports whether the runtime is up and has the model, separately from whether
the service itself is up — those are different questions and a single health check would confuse
them.

Ask for JSON that matches a schema:

```bash
curl -X POST http://localhost:8000/llm/structured   -H 'Content-Type: application/json'   -d '{
    "prompt": "Payments returns 500 on a third of requests with a NullReferenceException in PaymentProcessor.cs.",
    "prompt_name": "incident_summary",
    "json_schema": {
      "type": "object",
      "properties": {
        "summary": {"type": "string"},
        "affected_services": {"type": "array", "items": {"type": "string"}},
        "suspected_category": {"type": "string",
          "enum": ["database", "code_defect", "configuration", "dependency", "resource"]},
        "confidence": {"type": "number"}
      },
      "required": ["summary", "affected_services", "suspected_category", "confidence"]
    }
  }'
```

The schema is enforced three ways, weakest last: the model decodes under it, the response is
parsed out of whatever wrapping the model added, and a validation failure is fed back to the
model with the specific error before trying again. `enum` is carried all the way through — on a
3B model that is the difference between `code_defect` and a category it invented.

Every call becomes a `model_predictions` row with its latency and token counts, including calls
that never produced valid JSON. The AI service does not write that table: it owns the `rag`
schema, the backend owns `sentinel`, so it reports over the backend's `/internal` API and the
backend stores it. Both processes read one `INTERNAL_API_KEY`.

## MCP tools

Everything the agent can learn about the running system, it learns through an MCP server. There
are six, each a container, all read-only — 34 tools between them.

```bash
docker compose --profile core --profile samples --profile observability --profile mcp up -d --build
curl http://localhost:8000/mcp/tools | jq '.total_tools, .servers'
```

| Server | Port | Reads | Tools |
|---|---|---|---|
| logs-mcp | 7001 | Loki | 5 |
| metrics-mcp | 7002 | Prometheus | 7 |
| traces-mcp | 7003 | Jaeger | 5 |
| database-mcp | 7004 | PostgreSQL | 6 |
| git-mcp | 7005 | the repository | 6 |
| source-code-mcp | 7006 | the working tree | 5 |

Call one:

```bash
curl -X POST http://localhost:8000/mcp/call   -H 'Content-Type: application/json'   -d '{"tool": "metrics-mcp/get_service_metrics",
       "arguments": {"service": "orders", "minutes": 60}}'
```

Tools are named `server/tool` because the name alone is ambiguous: `logs-mcp/get_error_rate`
measures the share of log lines that are errors, and `metrics-mcp/get_error_rate` measures the
share of requests answered 5xx. Those are different numbers and they disagree — a service that
logs verbosely shows a low rate on the first while failing every request on the second.

Three things the tools do that a thin wrapper over each backend would not:

- **An empty result says so.** Six of the fifteen chaos scenarios produce no error log from the
  failing component, so `[]` would read to a model as "I failed to look". Every tool reports
  what it searched and that nothing matched.
- **Counting does not download.** `get_error_rate` counts with a LogQL metric query. Fetching
  the lines to `len()` them times out on a service that has been running for an afternoon.
- **The classification is enforced, not documented.** Each tool carries a `readOnlyHint`, the
  AI service's registry reads it, and the policy layer refuses anything not marked read-only
  without an approval token. A tool the registry has never seen is refused outright.

The **MCP Tools** page in the frontend lists all of it, shows which servers are reachable and
why not, and runs a tool with arguments seeded from its schema.

## Hybrid retrieval

Two retrievers over the same 28-document knowledge base — runbooks, architecture notes, service
docs, ten postmortems and two known errors — fused by reciprocal rank.

```bash
ollama pull bge-m3                     # ~1.2 GB; embeddings, served by the same Ollama

cd ai-service
.venv/Scripts/alembic upgrade head     # creates the rag tables inside the existing schema
.venv/Scripts/uvicorn app.main:app --port 8000

curl -X POST http://localhost:8000/rag/ingest -H 'Content-Type: application/json' -d '{}'
```

Ingestion is a mirror of `datasets/knowledge/`, not an append: an unchanged file is skipped
without being re-embedded, an edited one replaces its old chunks, and a deleted one leaves the
index. Running it on every start costs a directory walk.

```bash
curl -X POST http://localhost:8000/rag/search -H 'Content-Type: application/json' -d '{
  "query": "orders servisi yavasladi ama hic hata yok",
  "retriever": "hybrid",
  "k": 3
}'
```

That query is in Turkish and the corpus is in English. It returns INC-00003 and INC-00009 — the
two incidents where orders was slow with no errors, one a missing index and one an N+1 — because
bge-m3 puts both languages in the same vector space.

| | |
|---|---|
| Chunking | Markdown structure, ~400 tokens, 60 of overlap in whole blocks. A code fence is never split, and every chunk carries the heading path it sits under. |
| Lexical | BM25 in memory, with Lucene's IDF rather than the textbook one, which goes negative for a term in more than half the corpus. Identifiers are indexed whole *and* split, so "max pool size" finds `MaxPoolSize`. |
| Dense | bge-m3 through Ollama ([ADR-0004](docs/adr/0004-embeddings-through-ollama.md)), 1024 dimensions, HNSW over cosine. |
| Fusion | Reciprocal rank, k = 60. Rankings rather than scores: a cosine of 0.82 and a BM25 score of 11.4 are not comparable quantities. |
| Filters | `{"service": "orders", "document_type": ["runbook", "postmortem"]}`, evaluated identically in Python and in SQL — there is a test that fails if they ever disagree. |

Every search writes a `rag.retrieval_logs` row with both candidate lists and the per-stage
latencies, because which half proposed a chunk is the only thing that explains a surprising
hybrid result, and it cannot be reconstructed afterwards.

The hybrid retriever degrades rather than fails: if Ollama is down it answers from BM25 alone,
and `GET /rag/stats` says whether the embedding model is reachable. It raises only when *both*
halves fail — "the knowledge base has nothing about this" and "retrieval is broken" have to look
different to an agent.

## Reranking

Fusion is a fast filter. It has to get the right chunk into the top thirty; a cross-encoder then
decides which five of those thirty go in the prompt, reading the query and the chunk together
instead of comparing two vectors that were computed apart.

```bash
cd ai-service
pip install -e .[rerank]      # ~2.5 GB of PyTorch; bge-reranker-v2-m3 downloads on first use

# On a CUDA machine, replace the CPU wheel pip resolved by default. Worth 20 s a search.
pip install "torch==2.14.0+cu126" --index-url https://download.pytorch.org/whl/cu126

curl -X POST http://localhost:8000/rag/search -H 'Content-Type: application/json' -d '{
  "query": "connections pinned at a round number and p99 at the timeout",
  "retriever": "hybrid_rerank",
  "build_context": true
}'
```

The reranker is an optional dependency, because Ollama cannot serve a cross-encoder and the
alternative was 4.7 GB on a machine already holding a language model
([ADR-0005](docs/adr/0005-reranker-runs-in-process.md)). Without it a `hybrid_rerank` search
returns the fused order, says so — no `reranked` candidate list, and `rerank_available: false`
in `GET /rag/stats` — and the benchmark drops the row rather than reporting fused numbers under
the reranker's name.

`build_context: true` also returns what a prompt would actually contain: every chunk labelled
`[S1] runbook · orders · RB-001 · Runbook > Fix`, the chunker's block overlap removed so the
budget is not paid twice, and the whole thing capped at 3000 tokens. The label is what makes a
citation resolvable — an agent that cannot point at the chunk behind its conclusion is asserting
rather than citing — and the cap protects the incident's place in an 8k window.

## Measuring retrieval

```bash
cd ai-service
python -m evaluation.rag_eval --output run.json
```

A hundred and twenty labelled queries over the 28-document corpus
([`datasets/evaluation/`](datasets/evaluation/_README.md)), through every retriever, printed
as one table. The metrics are Recall@1/3/5, MRR and Precision@5, computed over **documents**
rather than chunks: ground truth is `runbooks/connection-pool-exhaustion.md`, which stays true
when the chunker changes, where a chunk id would go quietly wrong.

The query set is split by what each query tests, and the split is the interesting output —
`error_string` queries are exact stack traces where an embedding of `40P01` is close to nothing,
`cross_lingual` queries are Turkish against an English corpus where BM25 scores near zero, and
neither retriever owns both columns. That is the argument for the hybrid, made with numbers.

It was sixty queries for its first run and is a hundred and twenty now. The doubling is recorded
in [`datasets/evaluation/_README.md`](datasets/evaluation/_README.md), and so is the reason: at
sixty, two of the columns rested on eight queries each, so one query moved a column by 0.125 —
larger than most of the differences the table was being read for. Two of the conclusions below
are corrections of conclusions the smaller set supported.

The runner refuses to produce a misleading table: a query labelled with a document that is not
in the index stops the run rather than scoring every retriever equally and mysteriously worse,
and a query set with a duplicate id or an unlabelled query is rejected at load. Benchmark
searches are not written to `rag.retrieval_logs`, which exists to diagnose the agent's searches.

### What it says

A hundred and twenty queries, k=5, on one 16 GB machine with the cross-encoder on its GPU. R is
recall over documents; S is *success* — the share of queries with a relevant document in the top
k, which is what "how often is it right" usually means. R@1 cannot exceed **0.763** here, because
56 of the 120 queries have two relevant documents and one result cannot be both.

| retriever | R@1 | R@3 | R@5 | S@1 | S@5 | MRR | P@5 | p50 | misses |
|---|---|---|---|---|---|---|---|---|---|
| `hybrid_rerank` | 0.642 | **0.956** | **0.973** | **0.850** | **0.992** | **0.912** | 0.449 | 1.5 s | **1** |
| `hybrid` | 0.642 | 0.910 | 0.944 | **0.850** | 0.983 | 0.904 | 0.441 | 518 ms | 2 |
| `hybrid_hyde` | 0.642 | 0.890 | 0.944 | **0.850** | 0.983 | 0.905 | 0.431 | 5.8 s | 2 |
| `vector` | 0.642 | 0.856 | 0.890 | 0.833 | 0.967 | 0.890 | 0.419 | 522 ms | 4 |
| `bm25` | 0.522 | 0.785 | 0.829 | 0.692 | 0.867 | 0.768 | 0.388 | **0 ms** | 16 |

All five rows are one run of `--hyde`, which adds the expansion retrievers rather than replacing
anything, so the latencies are comparable to each other and slightly pessimistic for
`hybrid_rerank` — that run also had the 3B model resident.

The reranker earns its row, and it earns it in depth rather than in first place. R@3 goes 0.910
to 0.956: the question plain fusion answered at rank four or five, it answers at rank three.
Eleven queries improve and six get worse, and all six of those are a first place becoming a
second or a third — none leaves the top five, which is the only boundary that costs an answer.

Two things the sixty-query run said did not survive the doubling, and both are worth naming,
because a benchmark that only ever confirms itself is not being run.

**Reranking does not cost first place.** At sixty queries its S@1 was 0.817 against fusion's
0.867 and the table said so; at a hundred and twenty both are 0.850. That five-point penalty was
five queries out of sixty.

**It is not the retriever that finds everything.** At sixty it was the only row with no miss. It
still has the fewest — one — but that one, `Q084` (*"hata oranı testere dişi gibi inip çıkıyor"*),
is missed by all six, and it is a retrieval failure rather than a gap in the corpus:
`runbooks/database-deadlock.md` says *"error rate in a **sawtooth**: it spikes, recovers on its
own, spikes again"* in as many words. What nothing crosses is the metaphor — *testere dişi* to
*sawtooth*. All six return circuit-breaker documents instead, which do describe an error rate
that spikes and are the wrong answer, and BM25 returns nothing at all. It is the hardest query in
the set at precisely the property bge-m3 was chosen for.

It costs two to three times a fused search to do it: a p50 of 1.1–1.5 s across the three runs,
of which 518 ms is the fused search the cross-encoder is reordering. The spread is contention for
the GPU, and the 1.5 s in the table is the slow end, from the run that also held the 3B model.

The cross-encoder half of that took two corrections to get right, and both are in
[ADR-0005](docs/adr/0005-reranker-runs-in-process.md). `pip install torch` resolves a CPU-only
wheel without saying so, and 30 pairs of 512 tokens on a CPU is **21.5 s**, not the 300–600 ms
the ADR predicted. On the GPU at the precision the weights ship in it is 1.6–2.6 s. At float16 —
which `RERANK_DTYPE` picks on its own when the device is CUDA — it is 0.36–0.47 s, which is what
the original estimate was describing without knowing it. Recall was re-measured rather than
assumed at each step: over the sixty-query set every metric came out identical between the CPU
float32 run and the GPU float16 one, and exactly one query differed at all — in its fourth
result, between two documents that were both irrelevant.

### The split by query kind

Recall@5, by what the query is testing:

| retriever | cross_lingual | error_string | filtered | lookup | symptom |
|---|---|---|---|---|---|
| `bm25` | 0.385 | **1.000** | **1.000** | 0.932 | 0.900 |
| `vector` | 0.846 | 0.955 | 0.988 | 0.977 | 0.750 |
| `hybrid` | 0.846 | **1.000** | 0.988 | 0.955 | 0.950 |
| `hybrid_rerank` | **0.904** | **1.000** | 0.988 | **1.000** | **0.983** |

The split is still the argument for fusing two retrievers, but it is not the argument the
sixty-query run made. BM25 still takes `error_string` outright and still collapses on
`cross_lingual` — 0.385, by construction, since a Turkish question and an English corpus share
almost no tokens.

What reversed is `symptom`. Those queries describe a failure in an engineer's words rather than
the document's, and the dense half was supposed to own them; it scores 0.750 there, *below*
BM25's 0.900. A symptom written out in prose turns out to share more literal vocabulary with the
runbook than an embedding of the whole sentence preserves. Fused, the same queries score 0.950 —
a better argument for fusion than "each half owns a column" was, because neither half owns this
one and the fusion beats both.

Reranking then lifts precisely the two columns each half is worst at: `symptom` 0.950 to 0.983,
and `cross_lingual` 0.846 to 0.904.

### The cross-encoder is a vote, not a verdict

`--compare-rerank-modes` adds the row where the cross-encoder replaces the fused order instead of
being fused with it:

| | R@1 | R@3 | R@5 | S@1 | S@5 | MRR | P@5 | cross_lingual | misses |
|---|---|---|---|---|---|---|---|---|---|
| `hybrid_rerank` (blend) | 0.642 | **0.956** | **0.973** | 0.850 | **0.992** | 0.912 | 0.449 | **0.904** | **1** |
| `rerank_only` (replace) | **0.655** | 0.933 | 0.949 | **0.867** | 0.975 | **0.918** | **0.464** | 0.808 | 3 |

Replacing is better at the top of the ranking and worse below it. It wins R@1, S@1, MRR and
precision, and it loses three queries the blend keeps — one of them `Q031`, a Turkish question
plain fusion had at rank one. Its `cross_lingual` recall is 0.808: below the blend's 0.904 and
below plain fusion's 0.846, so the cross-encoder is the weaker of the two models at the one
property bge-m3 was chosen for. That is the same weakness the first run found, and smaller than
sixty queries made it look.

The blend wins the column that decides what a prompt contains. Five chunks go into the context;
whether the right document is first or third among them costs nothing, and whether it is there at
all costs the answer. So the cross-encoder is fused with the fused order rather than allowed to
overrule it — and `blend=False` stays runnable so that sentence stays checkable.

### One document does not get five slots

Adjacent chunks of one runbook score alike, so nothing stops a single document taking every slot
in a five-chunk result. `RETRIEVAL_MAX_CHUNKS_PER_DOCUMENT` caps it at two — two rather than one
because a runbook's symptom and its fix are different chunks and an investigation usually wants
both. `--no-diversity` measures what the cap is worth:

| | R@3 | R@5 | P@5 | misses |
|---|---|---|---|---|
| `hybrid_rerank`, capped | **0.956** | **0.973** | 0.449 | 1 |
| `hybrid_rerank`, uncapped | 0.951 | 0.964 | **0.490** | 1 |
| `hybrid`, capped | **0.910** | **0.944** | 0.441 | 2 |
| `hybrid`, uncapped | 0.906 | 0.939 | **0.485** | 2 |

About a point of recall@5 for four of precision@5 — and it rescues no query that was otherwise
lost: the miss lists are identical with the cap and without it. What it recovers is the *second*
relevant document of a query that has two, which is 56 of these 120, and is exactly what an
investigation that wants both the runbook and the postmortem needs. The precision it gives up is
chunk-level duplication: a second chunk of a document already retrieved scores as relevant and
tells the agent nothing new.

### Query expansion, and why it is off

[`rag/expansion.py`](ai-service/rag/expansion.py) implements HyDE: ask the 3B model for the
passage that *would* answer the question, and search with that too. It was built for four
measured failures where the question and its answer share almost no vocabulary — a search for
"what is the label called" that never reaches a document saying `{service_name="orders"}`.

It closed the gap it was built for. `Q047` — "how do I query the logs of a service, and what is
the label called", which no retriever without it finds at all — comes back at rank 4; `Q042` goes
from 4 to 2, `Q014` from 5 to 4. Eleven queries improve.

Eight get worse, and one of the eight is `Q035`, from rank one to unfound: a 3B model writing an
English probe from a Turkish question sometimes writes about a different service, and fusion
protects the answer from that but not the top of the ranking. R@5 ties plain fusion at 0.944, R@3
is worse at 0.890, and it costs eleven times the latency.

So `HYDE_ENABLED` is `false`, and the reason is now sharper than "it loses on average" — because
over a hundred and twenty queries it does not lose on average. **The reranker closes the same gap
better and four times cheaper.** `Q047` unfound → rank 3, against HyDE's 4. `Q014` 5 → 3, against
4. `Q038` 4 → 3, where HyDE left it at 4. Only `Q042` is a query HyDE places higher. Generating a
probe was the expensive way to reach a document whose vocabulary the question did not share;
reading the query and the chunk together is the cheap one.

The code stays: `--hyde` reproduces the row and `"retriever": "hybrid_hyde"` runs it per search.
A measured negative result is worth more than an untested feature flag, and this one is now the
evidence for a design decision rather than just a discarded idea.

## Tests

```bash
dotnet test backend/Sentinel.sln              # 18 unit, 28 integration (Testcontainers)
cd frontend && npm test                       # 12 component tests
cd mcp-servers && pytest                       # 64 unit
cd ai-service && pytest                        # 232 unit, 23 against PostgreSQL
```

The integration tests run the API against a real PostgreSQL started for the run, using the same
image and the same init scripts as compose. That is not thoroughness for its own sake: the two
bugs that shipped in Phase 1 were a generated column written as an empty string and a filtered
index whose predicate PostgreSQL rejected, and neither would fail against an in-memory provider.
Both now have a test named after them.

The MCP servers additionally have a live suite covering all 34 tools against the real backends,
skipped unless the stack is up:

```bash
docker compose --profile core --profile samples --profile observability up -d
cd mcp-servers && LIVE_MCP=1 pytest tests/test_live.py
```

What it adds is the only thing that cannot be faked: that the query each tool sends is one the
real Loki, Prometheus, Jaeger or PostgreSQL accepts. A LogQL filter in the wrong clause passes
every unit test and returns nothing in production.

## Layout

```text
backend/          ASP.NET Core 8, Clean Architecture (Domain / Application / Infrastructure / Api)
frontend/         React 19, TypeScript, Vite, Tailwind, TanStack Query, Zustand
ai-service/       Python FastAPI: local LLM, structured output, prompts, hybrid RAG (agent later)
mcp-servers/      6 read-only MCP servers, one image, six entrypoints   (2 more in Phase 10)
sample-services/  5 .NET microservices with chaos middleware, plus an uninstrumented
                  third-party delivery provider for scenario 12
infrastructure/   PostgreSQL init, Prometheus, Grafana, Loki, OTel
datasets/         The seed knowledge base and the retrieval query set; routing data in Phase 8
docs/             Planning, ADRs, architecture notes
```

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: repo, database, sample services, backend, frontend | **Done** |
| 2 | Observability: OTel, Loki, Prometheus, Jaeger, Grafana, chaos behaviour | **Done** |
| 3 | Local LLM provider and structured output | **Done** |
| 4 | MCP infrastructure, read-only tools | **Done** |
| 5 | Hybrid RAG | **Done** |
| 6 | Reranker and retrieval evaluation | **Done** |
| 7 | Investigation agent | **Done** |
| 8 | Fine-tuned router | **Done** |
| 9 | Incident memory | **Done** |
| 10 | Human-in-the-loop remediation | **Done** |
| 11 | Evaluation dashboard | In progress |
| 12 | Polish, docs, CI | |

Each phase has a done criterion in [docs/planning.md](docs/planning.md) and is not left until it
is met.
