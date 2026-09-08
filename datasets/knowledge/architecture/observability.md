---
type: architecture
title: Where the telemetry is and what it is called
id: ARCH-003
---

# Where the telemetry is and what it is called

## Metrics (Prometheus, scraped from each service's `/metrics`)

| Metric | What it is |
|---|---|
| `http_server_request_duration_seconds` | Histogram, labelled `job`, `http_route`, `http_response_status_code`. p95 and p99 come from its buckets. |
| `http_server_active_requests` | Gauge. A climbing value with flat throughput means requests are piling up somewhere. |
| `process_working_set_bytes` | Resident memory. **The leak signal.** |
| `dotnet_gc_heap_size_bytes` | Split by generation — summing it by job gives generations of one service, not a total across services. |
| `dotnet_gc_collections_total` | Gen-2 collections climbing alongside working set confirms a leak rather than a cache warming up. |
| `process_cpu_utilization` | Fraction of one core, per service. |

Error rate is derived, not exported:

```promql
sum by (job) (rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))
/ sum by (job) (rate(http_server_request_duration_seconds_count[5m]))
```

A service with traffic and no errors has no numerator series at all, so the expression needs
`or vector(0)` to draw a zero rather than nothing. An empty panel is not the same finding as a
flat zero, and reading one as the other has cost real time.

## Logs (Loki, via the OTel collector)

Structured, with `service.name`, `trace_id` and `span_id` on every line. LogQL:

```logql
{service_name="orders"} |= "Exception" | json | line_format "{{.body}}"
```

Because `trace_id` is on the log line and on the span, a failing trace found in Jaeger can be
turned into its log lines without a text search.

## Traces (Jaeger, OTLP)

Every hop, including database calls through the Npgsql instrumentation. Useful queries are by
operation (`POST /api/checkout`), by minimum duration, and by error status.

## What is not instrumented

Two things, and both matter when reading a trace:

- **Connection acquisition.** Waiting for a pooled connection appears as a gap before the DB
  span, not as a span of its own.
- **The stub delivery gateway in notifications.** It is an external span with no server side, so
  a failure there terminates the trace at the boundary.
