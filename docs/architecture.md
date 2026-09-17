# Architecture

How SentinelAI is put together, and why the seams are where they are. The individual decisions
that were hard enough to argue about live in [adr/](adr/); this is the map that makes them make
sense.

## The shape

```text
                     ┌─────────────────────────────┐
                     │  React + TypeScript          │  :5173
                     │  8 pages + a live timeline   │
                     └──────────┬──────────────────┘
                       REST     │  SignalR
                     ┌──────────▼──────────────────┐
                     │  ASP.NET Core 8              │  :5080
                     │  Api / Application /         │
                     │  Domain / Infrastructure     │
                     └──────┬───────────────┬──────┘
              HTTP + callbacks              │  EF Core
                     ┌──────▼───────┐   ┌───▼──────────────────────┐
                     │ Python       │   │ PostgreSQL 17 + pgvector │
                     │ FastAPI      │   │ sentinel · rag · svc_*   │
                     │ :8000        │   └───▲──────────────────────┘
                     └──┬────────┬──┘       │ SQLAlchemy
          MCP (HTTP)    │        │  HTTP    │
              ┌─────────▼──┐  ┌──▼───────┐  │
              │ 8 MCP      │  │ Ollama   │  │
              │ servers    │  │ :11434   │  │
              │ ~43 tools  │  └──────────┘  │
              └─────┬──────┘                │
                    │ queries               │
     ┌──────────────▼───────────────────────▼──────────┐
     │ Loki · Prometheus · Jaeger · Docker · git · fs   │
     └──────────────▲──────────────────────────────────┘
                    │ OTLP
     ┌──────────────┴──────────────────────────────────┐
     │ 5 sample microservices + chaos middleware        │
     │ gateway · users · orders · payments · notifications
     └──────────────┬──────────────────────────────────┘
                    │ HTTP
              ┌─────▼──────────────┐
              │ delivery-provider  │  uninstrumented, on purpose
              └────────────────────┘
```

Everything runs locally. No hosted model, nothing billed.

## Two runtimes, and why

The backend is .NET and the reasoning service is Python, which is one more runtime than a project
this size would normally carry. The split is along a real line: the backend owns identity,
persistence, the incident lifecycle and the API the browser talks to, and the AI service owns
everything that needs the Python ML ecosystem — the model client, structured decoding, the
embedding and reranking stack, and the agent itself. Neither half is a thin wrapper over the
other; each is the natural home for what it holds.

The cost is a boundary, and the boundary is where the interesting decisions are.

### One database, six schemas

| schema | owner | migrations |
|---|---|---|
| `sentinel` | backend | EF Core |
| `rag` | AI service | Alembic |
| `svc_users`, `svc_orders`, `svc_payments`, `svc_notifications` | sample services | EF Core, one each |

One instance so there is one thing to back up and one to start. Separate schemas so
`database-mcp` can attribute a slow query, a lock or connection pressure to a *service* —
without that attribution, chaos scenarios 1, 2, 3 and 4 look identical from the database side.

**Neither application writes to the other's schema.** The AI service records every model call it
makes for an investigation — the `/llm` endpoint, the reasoning nodes, the critic and the
postmortem writer — but `model_predictions` is a `sentinel` table, so it reports over the
backend's `/internal` API and the backend stores it. That is one more hop than writing the row directly, and it keeps the
rule that a schema has exactly one writer.

### Callbacks, not a queue

An investigation is minutes of work that the browser watches happen. The AI service pushes state
transitions back to the backend over HTTP, and the backend relays them to the browser over
SignalR. A message broker would be the textbook answer and it is not here: see
[ADR-0002](adr/0002-callback-over-queue.md) for the argument, and
[ADR-0006](adr/0006-callback-resilience.md) for what happens when a callback fails, which is the
part that matters more.

## The investigation

A hand-written state machine rather than a graph framework
([ADR-0003](adr/0003-custom-state-machine-over-langgraph.md)). The bet was that the interesting
part is not edge declaration — it is that every transition is an event a human watches arrive,
and that the loop provably ends.

```text
UNDERSTAND_INCIDENT → PLAN → ┌─ COLLECT_LOGS      ─┐
                             │  COLLECT_METRICS    │
                             │  COLLECT_TRACES     │
                             │  COLLECT_DATABASE   │ ← the plan picks which,
                             │  CHECK_DEPLOYMENTS  │   and in what order
                             │  SEARCH_HISTORY     │
                             └─ INSPECT_CODE      ─┘
                                        │
                        GENERATE_HYPOTHESES ⇄ COLLECT_ADDITIONAL_EVIDENCE
                                        │      (up to 3 rounds)
                              RANK_HYPOTHESES
                             SELECT_ROOT_CAUSE
                                   VALIDATE ──rejected──┐
                                        │               │
                              RECOMMEND_FIX             └→ back to hypotheses
                                        │
                    COMPLETED · NEEDS_HUMAN · FAILED
```

Four guarantees the runner enforces, none of which a node can opt out of:

* **It stops.** A transition ceiling above the semantic limits, because two nodes pointing at
  each other spend no budget and would otherwise run until the process died.
* **Running out of tool budget is not a failure.** It ends in `NEEDS_HUMAN`, because evidence
  without a conclusion is worth showing to somebody.
* **An exception is recorded, not swallowed.** An investigation that dies silently is worse than
  one that dies loudly; the incident is still open either way.
* **A transition to a state with no node is a defect**, not something to skip past.

`VALIDATE` is a critic that can reject a conclusion and send the machine back for another round.
It has to give grounds — [ADR-0007](adr/0007-critic-veto-needs-grounds.md) is what happened when
it did not.

### Routing

A 1.5B model fine-tuned with QLoRA turns the question into an intent and a collector plan. It
routes 0.871 of a 333-question test set correctly against the keyword table's 0.354 — and the
keyword table stays behind it as a fallback, so a machine that has never run
`ollama create sentinel-router` keeps working with the accuracy it had before.

## The tool surface

Eight MCP servers over Streamable HTTP, one image and eight entrypoints. They are the only way
the agent touches anything outside itself.

| server | reads | notable tools |
|---|---|---|
| `logs-mcp` | Loki | `get_recent_errors`, `search_logs`, `get_exception_statistics` |
| `metrics-mcp` | Prometheus | `get_error_rate`, `get_response_time`, `get_cpu_usage` |
| `traces-mcp` | Jaeger | `get_slowest_spans`, `get_failed_traces`, `get_service_dependencies` |
| `database-mcp` | PostgreSQL | `get_slow_queries`, `get_connection_count`, `get_locks_and_deadlocks` |
| `git-mcp` | the repository, read-only | `get_recent_commits`, `get_commit_diff`, `get_file_history` |
| `source-code-mcp` | the repository, read-only | `read_file`, `search_code` |
| `docker-mcp` | the Docker socket, via a proxy | `get_container_stats`, `restart_container` |
| `testing-mcp` | the running services | drives real traffic to check whether a fix worked |

**Read-only and destructive tools are different things.** A destructive tool requires an approval
token that names one action: it is an HMAC over the recommendation, the tool and a hash of the
arguments, with an expiry. Approving "restart the orders container" authorises that and nothing
else. The server verifies the token a second time against the same secret, because this client is
not the only thing that can reach a container on the compose network.

`docker-mcp` never sees the Docker socket directly — a proxy in front of it allows the endpoints
it needs and refuses the rest.

## Retrieval

Four retrievers behind one interface — BM25, dense, hybrid with reciprocal rank fusion, and
hybrid plus a cross-encoder reranker — measured against the same 120-query set rather than
asserted. `hybrid_rerank` reaches R@5 0.973.

The reranker runs in process rather than through Ollama, which cannot serve a cross-encoder
([ADR-0005](adr/0005-reranker-runs-in-process.md)); embeddings do go through Ollama
([ADR-0004](adr/0004-embeddings-through-ollama.md)). Query expansion was built, measured, and
left off: it improves eleven queries and worsens eight, and the reranker closes the same gap four
times cheaper. The code stays as the evidence for the decision.

A concluded investigation writes its own postmortem into the knowledge base, so the next incident
can find it. It is retrieved like any other document.

## What there is to investigate

Five .NET sample microservices with chaos middleware, and
[fifteen failure scenarios](../sample-services/chaos/scenarios.md) that genuinely break them.
Enabling one changes what the service does; the telemetry is a side effect, exactly as in
production. Six of the fifteen produce no error log at all from the failing component, so an
agent that only reads logs cannot solve them.

Every scenario obeys four rules, and the third is the one that keeps costing work to honour:
enable and disable must be idempotent, and **state must not outlive the scenario that created
it**. Three separate faults in this system broke that rule and each one corrupted a later
measurement — a circuit breaker that stayed open into the next case's baseline, a leak whose
retained memory would have, and two million padding rows that turned a connection-pool demo into
a slow-query diagnosis.

`delivery-provider` is the sixth container and the only process that exports no telemetry. That
absence is deliberate: scenario 12 is recognised by a failure that terminates at an *external*
span, and an instrumented provider would appear in Jaeger as another service and read as an
internal dependency instead.

## Observability, including of itself

The services export traces, metrics and logs over OTLP to one collector, which fans them out to
Jaeger, Prometheus and Loki. One ingress, so swapping any of the three is a change in the
collector's configuration rather than in nine services.

**The backend and the AI service report into the same three.** They emit the standard HTTP
semantic conventions, so they appear in the `Service Health` dashboard beside the services they
investigate; on top of that the AI service records what only it has — investigations by outcome
and duration, where an investigation's minutes went by state, model latency and tokens, MCP tool
calls by server and outcome, and retrieval latency. That is the `SentinelAI Itself` dashboard.

Both are silent unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so either runs against a bare
database with no collector to talk to.

## Where the numbers come from

`python -m evaluation.suite` runs every benchmark and writes one record per run under
`datasets/evaluation/runs/`. The suite calls the evaluators rather than reimplementing them —
copied scoring would be a second definition of recall, and the two would disagree exactly once.

There is deliberately **no endpoint that starts a benchmark**. A run is minutes of GPU, and a page
that can start one can start two during an incident. A test asserts the absence.

The agent benchmark breaks a service for real and scores the conclusion. Its headline is the
false remediation rate — runs that concluded wrongly and still proposed an executable fix, which
is a bad answer somebody can click. Reproduction is checked with four instruments, because no one
of them sees every fault: failures, throughput collapse, per-request latency, and container
memory. The fourth exists because the memory leak trips none of the first three.

## Layout

```text
backend/          ASP.NET Core 8: Api · Application · Domain · Infrastructure
ai-service/       FastAPI: llm · rag · routing · agents · mcp_client · evaluation · observability
frontend/         React + TypeScript + Tailwind: 8 pages, plus the investigation
                  detail view where the timeline arrives over SignalR
mcp-servers/      8 servers, one image, eight entrypoints
sample-services/  5 microservices + chaos middleware, and the delivery provider
infrastructure/   PostgreSQL init, Prometheus, Grafana, Loki, OTel collector
datasets/         the seed corpus, the query sets, and every benchmark run
docs/             planning, ADRs, this file
scripts/          demo.py, dev.ps1
```
