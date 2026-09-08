# ADR-0001: Grafana Loki as the log store, not OpenSearch

- **Status:** Accepted
- **Date:** 2026-09-08
- **Deciders:** project owner

## Context

The requirements diagram shows OpenSearch as the log store, but OpenSearch does not appear in
the technology list, so the choice was never actually settled. SentinelAI needs a log backend
that `logs-mcp` can query on the agent's behalf for `get_service_logs`, `search_logs`,
`get_recent_errors` and `get_exception_statistics`.

The binding constraint is the development machine: 16 GB RAM, and the full stack eventually runs
PostgreSQL, the log store, Prometheus, Jaeger, Grafana, 8 MCP servers, 5 sample services, the
backend, the frontend and a local LLM at the same time.

## Decision

Use **Grafana Loki 3** behind an OpenTelemetry Collector, queried through LogQL.

Log access in the AI service sits behind an `ILogStore` abstraction so the backend can be
swapped without touching agent code.

## Consequences

**Positive**

- Loki runs as a single binary in roughly 200 MB of RAM; OpenSearch wants 2–4 GB before it is
  useful. On a 16 GB machine that difference decides whether the stack starts at all.
- Native Grafana integration means log, metric and trace panels share one UI with no extra work.
- Label-based indexing fits the query shape the agent actually uses: filter by service and time
  window, then grep the stream.

**Negative**

- Loki indexes labels, not content. Full-text search across a wide time range is slower than
  OpenSearch would be, and `search_logs` has to be scoped by service and time window to stay
  responsive. This is an acceptable restriction because every agent query is already scoped that
  way.
- No aggregations as rich as OpenSearch's. `get_exception_statistics` is implemented with LogQL
  pattern extraction plus counting rather than a term aggregation.

**Neutral**

- Retention is set deliberately short (24–48 hours). Investigations look at minutes-to-hours
  windows, so nothing is lost, and disk stays small.

## Alternatives considered

- **OpenSearch** — better search, rejected on memory footprint.
- **PostgreSQL full-text as the log store** — one less container, but no realistic observability
  story to demonstrate, and the project's whole point is showing that the agent works against
  real production-shaped tooling.
