---
type: architecture
title: Sample estate architecture
id: ARCH-001
---

# Sample estate architecture

Five .NET 8 minimal-API services behind one gateway, sharing a single PostgreSQL 17 instance
with one schema each. This is the system SentinelAI investigates; it is not SentinelAI itself.

## Components

| Service | Port | Schema | Owns |
|---|---|---|---|
| gateway | 8080 | none | The edge. Fans out to the other four; holds no data of its own. |
| users | 8081 | `svc_users` | User records. |
| orders | 8082 | `svc_orders` | Orders and order lines. Calls payments. |
| payments | 8083 | `svc_payments` | Payment authorisation. |
| notifications | 8084 | `svc_notifications` | Delivery of notifications through a stub gateway. |

Every service runs the shared `Sentinel.Samples.Common` middleware, which gives it the same
health endpoint, the same OpenTelemetry configuration, the same `/metrics` exposition, and the
same chaos API.

## Why one database with five schemas

One container, one backup, one connection string template. The separation is by schema rather
than by instance so that `database-mcp` can attribute a slow query, a lock or a connection count
to a specific service: `pg_stat_statements` rows carry the schema, and without that attribution
the four database failure scenarios all look identical from the database side.

The cost is that the services share a connection budget. `max_connections` is 300 for the whole
instance, and a service that exhausts its own Npgsql pool is not exhausting the server's — which
is exactly the distinction the connection pool runbook turns on.

## Data flow

The one flow that crosses every service is checkout:

```text
POST /api/checkout  →  gateway
  → GET  users/{id}          (does the user exist)
  → POST orders              (create the order)
      → POST payments/authorize   (authorise it)
  → notifications is written to asynchronously afterwards
```

A failure anywhere in that chain surfaces at the gateway as a 5xx, which is why the gateway's
error rate is the least useful signal in the system for locating a cause and the most useful for
detecting one.

## Telemetry

All five export OTLP to the collector, which fans out to Loki (logs), Prometheus (metrics via
scrape) and Jaeger (traces). Trace context propagates across every HTTP hop, so a checkout
produces one trace with four services in it.
