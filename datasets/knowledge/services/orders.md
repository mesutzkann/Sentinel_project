---
type: service_doc
title: orders service
service: orders
id: SVC-orders
---

# orders service

Port 8082, schema `svc_orders`. The busiest service and the one with the most ways to fail: it
has a database, a downstream call, and the largest table in the estate.

## Endpoints

| Endpoint | Notes |
|---|---|
| `GET /orders` | Paged list, `limit` clamped to 200. |
| `GET /orders/{id}` | Order with its lines. The eager loading here is what scenario 4 removes. |
| `POST /orders` | Creates the order, then calls `payments/authorize`. |

## Dependencies

- PostgreSQL, schema `svc_orders`. Tables `orders` and `order_lines`.
- payments, over HTTP, with a 30 second client timeout and a retry policy.

## Connection pool

Npgsql's `MaxPoolSize` is 200 and that is what the connection string carries. The pool is per
process, not per database: exhausting it does not exhaust PostgreSQL's `max_connections`, which
is 300 for the whole instance. That distinction decides an investigation — a service starved of
connections while the database reports plenty of headroom is a pool problem, not a database
problem, and the two have different fixes.

## How it fails

- **Slow, no errors, one long DB span.** A query that lost its index. `get_slow_queries` ranks it
  first by total time; `describe_table` shows nothing covering the predicate.
- **Slow, no errors, 50+ short DB spans in one trace.** N+1. The individual query is trivial and
  its call count is enormous.
- **Slow with timeouts, and the wait sits before the DB span.** Pool exhaustion.
- **Slow with CPU pinned and no DB or HTTP time at all.** Compute in the request path.

The first two are indistinguishable from metrics alone — both are "p95 is up, error rate is
flat". The trace separates them, and nothing else does: one long span versus many short ones.

## Chaos scenarios owned

`DB_CONNECTION_POOL_EXHAUSTION` (1), `DB_SLOW_QUERY_MISSING_INDEX` (2), `DB_N_PLUS_ONE_QUERY` (4),
`DIVIDE_BY_ZERO_EDGE_CASE` (6), `CPU_SATURATION` (15).
