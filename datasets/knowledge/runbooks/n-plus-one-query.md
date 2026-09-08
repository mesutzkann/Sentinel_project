---
type: runbook
title: Runbook — N+1 query
service: orders
id: RB-004
scenario: DB_N_PLUS_ONE_QUERY
severity: medium
---

# Runbook — N+1 query

## Symptom

Latency rises moderately. Error rate is flat. Nothing in the logs. Database CPU rises sharply
relative to the request rate — the database is doing much more work per request than it was, and
none of that work is individually slow.

## Confirm it

1. A trace of one request: **50 or more sibling database spans**, each of them fast. That is the
   whole diagnosis; nothing else is as direct.
2. `get_slow_queries` — a trivial query, low mean time, enormous call count. Sort by call count
   rather than by mean time or it will not be near the top.
3. `search_code` on the endpoint — a loop that queries per item, or an `Include` that was
   removed from the query that loads the aggregate.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Missing index | One slow query there; here it is many fast ones. Compare the trace shapes. |
| Pool exhaustion | Errors and timeouts there, and the wait is before the span. |
| CPU saturation in the service | The CPU rise is in PostgreSQL here, not in the .NET process. |

Ratio of database spans to requests is the number to look at. Healthy is one to three per
request; this failure produces one per row.

## Fix

Restore eager loading, so the aggregate and its children come back in one query:

```csharp
await db.Orders
    .Include(o => o.Lines)
    .FirstOrDefaultAsync(o => o.Id == id, cancellationToken);
```

Watch for the opposite mistake while fixing it: a chain of `Include` calls on a collection
produces a cartesian product. `AsSplitQuery()` is the tool for that, and it is two queries rather
than fifty.

## Verify

The same trace should now contain two or three database spans. Database CPU falls back in step
with it.

## Prevent

Assert on query count in an integration test for the endpoints that load aggregates. It is the
only check that fails at the moment the `Include` is removed rather than a month later under
load.
