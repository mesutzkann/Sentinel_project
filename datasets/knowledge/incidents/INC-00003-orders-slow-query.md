---
type: postmortem
title: INC-00003 — orders slow with no errors after a filter change
service: orders
id: INC-00003
scenario: DB_SLOW_QUERY_MISSING_INDEX
severity: medium
date: 2026-04-07
duration_minutes: 190
resolution: schema_change
---

# INC-00003 — orders slow with no errors after a filter change

## Summary

For three hours the order list endpoint took 600–900 ms instead of its usual 45 ms. Nothing
failed, nothing was logged, and no alert fired. A query had been changed to filter on a column
with no index, turning an index scan into a sequential scan over two million rows.

## Timeline

| Time | Event |
|---|---|
| 11:20 | Release changes the order list filter from `status` to `channel`. |
| 11:26 | p95 crosses 500 ms. No alert exists for latency; nobody is paged. |
| 13:40 | A support ticket about a slow page starts the investigation. |
| 14:05 | `get_slow_queries` ranks the statement first by total time. |
| 14:12 | `describe_table` shows no index containing `channel`. |
| 14:30 | `CREATE INDEX CONCURRENTLY` completes. |
| 14:31 | p95 back to 48 ms. |

## What we saw

Latency up, error rate exactly flat, and **nothing at all in the logs**. Loki had no error, no
warning, nothing to search for. This class of failure is invisible to a log-only investigation:
there is no line to find.

The trace showed a single database span accounting for nearly all of the request. One long span,
not many short ones — that distinction is what separated this from an N+1, which we saw later in
INC-00009 and which looks identical in every metric.

## Root cause

The new predicate filtered on `channel`, which no index covered. `EXPLAIN` confirmed a sequential
scan over the full `svc_orders.orders` table.

## What made it hard

Two hours and twenty minutes passed before anyone looked, because the only alert on this service
was on error rate and the error rate never moved. The failure was found by a customer, not by
monitoring.

## Fix

```sql
CREATE INDEX CONCURRENTLY ix_orders_channel_created_at
    ON svc_orders.orders (channel, created_at DESC);
```

`CONCURRENTLY` so the table stayed writable during the build.

## Follow-up

Added a p95 latency alert per service. Query plans for changed predicates are now checked against
the indexes before release.

See also: [Runbook — slow query from a missing index](../runbooks/slow-query-missing-index.md),
and INC-00009, which had the same symptom and a different cause.
