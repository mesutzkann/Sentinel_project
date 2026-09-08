---
type: runbook
title: Runbook — slow query from a missing index
service: orders
id: RB-002
scenario: DB_SLOW_QUERY_MISSING_INDEX
severity: medium
---

# Runbook — slow query from a missing index

## Symptom

p95 and p99 latency rise. **Error rate does not move at all**, and there is nothing in the logs
— no exception, no warning, nothing. The service is working correctly and slowly.

This is the failure that a log-only investigation cannot find. There is no line to search for.

## Confirm it

1. `get_slow_queries` — one statement at the top by *total* time, with a mean time in the
   hundreds of milliseconds and a modest call count.
2. `describe_table` on the table it reads — no index covering the predicate in the `WHERE`
   clause.
3. A trace of the slow endpoint: a single database span dominating the whole request. One long
   span, not many.

`EXPLAIN` on the statement shows a sequential scan over the full table where an index scan is
expected.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| N+1 query | Many *fast* queries with a huge call count, not one slow one. Check the trace: 50+ sibling DB spans versus one long one. |
| CPU saturation | Time is inside the DB span here; there it is in an internal compute span with no database involvement. |
| Pool exhaustion | Errors accompany that one, and the wait is before the span rather than inside it. |

## Fix

Create the index. Concurrently, so the table is not locked against writes:

```sql
CREATE INDEX CONCURRENTLY ix_orders_status_created_at
    ON svc_orders.orders (status, created_at DESC);
```

`CONCURRENTLY` cannot run inside a transaction block and takes longer. On a two million row
table it is worth both.

## Verify

Re-run `get_slow_queries`: the statement should fall out of the top by total time. p95 back
under 100 ms. `EXPLAIN` shows an index scan.

## Prevent

Alert on p95 latency rather than only on error rate. Everything in this class of failure is
invisible to an error-rate alert, and this is the reason latency alerts exist.
