# SentinelAI

Autonomous incident investigation and resolution platform. Given an incident on a microservice,
it investigates the way an on-call engineer would — reading logs, metrics, traces, recent
commits and source code — proposes a root cause with a confidence score, and, once a human
approves, applies the fix and verifies that it worked.

Everything runs locally. No hosted model, no paid API.

> **Status: Phase 5 of 12.** Foundation, observability, the local LLM layer, the MCP tool
> surface and hybrid retrieval. See
> [docs/planning.md](docs/planning.md) for the full roadmap and
> [Phase status](#phase-status) for what works today.

## What makes it interesting

- **A visible agent state machine.** Written by hand rather than assembled from a framework
  ([ADR-0003](docs/adr/0003-custom-state-machine-over-langgraph.md)), so every transition is an
  event the UI can draw and a person can reason about.
- **A fine-tuned router.** A 1.5B model trained with QLoRA to turn a question into an intent and
  a tool plan in under 150ms, benchmarked against its own base model.
- **Four retrievers behind one interface.** BM25, dense, hybrid with reciprocal rank fusion, and
  hybrid plus cross-encoder reranking — measured against the same query set rather than asserted.
  Three of the four are in; `POST /rag/search` takes the retriever by name, so the comparison is
  something you run rather than something you are told.
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

## Observability

```bash
docker compose --profile core --profile samples --profile observability up -d --build
```

The five services export traces, metrics and logs over OTLP to one collector, which fans them
out to Jaeger, Prometheus and Loki. Grafana arrives with all three wired up and a provisioned
dashboard — nothing is clicked together by hand.

| | | |
|---|---|---|
| Grafana | <http://localhost:3000> | `Service Health` dashboard, under the SentinelAI folder |
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

Five of the fifteen scenarios have their behaviour implemented
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

## Tests

```bash
dotnet test backend/Sentinel.sln              # 18 unit, 28 integration (Testcontainers)
cd frontend && npm test                       # 12 component tests
cd mcp-servers && pytest                       # 64 unit
cd ai-service && pytest                        # 135 unit, 23 against PostgreSQL
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
sample-services/  5 .NET microservices with chaos middleware
infrastructure/   PostgreSQL init, Prometheus, Grafana, Loki, OTel
datasets/         The seed knowledge base; routing data and eval fixtures (Phase 8+)
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
| 6 | Reranker and retrieval evaluation | Next |
| 7 | Investigation agent | |
| 8 | Fine-tuned router | |
| 9 | Incident memory | |
| 10 | Human-in-the-loop remediation | |
| 11 | Evaluation dashboard | |
| 12 | Polish, docs, CI | |

Each phase has a done criterion in [docs/planning.md](docs/planning.md) and is not left until it
is met.
