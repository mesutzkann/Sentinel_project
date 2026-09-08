# SentinelAI

Autonomous incident investigation and resolution platform. Given an incident on a microservice,
it investigates the way an on-call engineer would — reading logs, metrics, traces, recent
commits and source code — proposes a root cause with a confidence score, and, once a human
approves, applies the fix and verifies that it worked.

Everything runs locally. No hosted model, no paid API.

> **Status: Phase 3 of 12.** Foundation, observability and the local LLM layer. See
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

## Layout

```text
backend/          ASP.NET Core 8, Clean Architecture (Domain / Application / Infrastructure / Api)
frontend/         React 19, TypeScript, Vite, Tailwind, TanStack Query, Zustand
ai-service/       Python FastAPI: local LLM, structured output, prompts (agent and RAG later)
mcp-servers/      8 MCP servers, one image with different entrypoints    (Phase 4+)
sample-services/  5 .NET microservices with chaos middleware
infrastructure/   PostgreSQL init, Prometheus, Grafana, Loki, OTel
datasets/         Routing training data and evaluation fixtures          (Phase 8+)
docs/             Planning, ADRs, architecture notes
```

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundation: repo, database, sample services, backend, frontend | **Done** |
| 2 | Observability: OTel, Loki, Prometheus, Jaeger, Grafana, chaos behaviour | **Done** |
| 3 | Local LLM provider and structured output | **Done** |
| 4 | MCP infrastructure, read-only tools | Next |
| 5 | Hybrid RAG | |
| 6 | Reranker and retrieval evaluation | |
| 7 | Investigation agent | |
| 8 | Fine-tuned router | |
| 9 | Incident memory | |
| 10 | Human-in-the-loop remediation | |
| 11 | Evaluation dashboard | |
| 12 | Polish, docs, CI | |

Each phase has a done criterion in [docs/planning.md](docs/planning.md) and is not left until it
is met.
