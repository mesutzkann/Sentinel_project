---
type: postmortem
title: INC-00009 — orders slow again, and it was not the index this time
service: orders
id: INC-00009
scenario: DB_N_PLUS_ONE_QUERY
severity: medium
date: 2026-07-12
duration_minutes: 143
resolution: code_fix
---

# INC-00009 — orders slow again, and it was not the index this time

## Summary

The order detail endpoint slowed from 50 ms to about 400 ms for two and a half hours. In metrics
it was indistinguishable from INC-00003, and the first hour was spent looking for a missing
index that did not exist. The actual cause was an `Include` removed during a refactor, turning
one query into one per order line.

## Timeline

| Time | Event |
|---|---|
| 09:50 | Refactor deployed. `Include(o => o.Lines)` dropped from the detail query. |
| 10:04 | p95 alert fires — the alert added after INC-00003. |
| 10:10 | Investigation opens against the INC-00003 runbook. |
| 10:38 | `get_slow_queries` shows nothing slow. Mean times are all under 2 ms. |
| 11:05 | Sorted by call count instead: one trivial query called 340,000 times an hour. |
| 11:20 | A trace shows 60+ sibling database spans in a single request. |
| 12:13 | Eager loading restored and deployed. |

## What we saw

Latency up, error rate flat, nothing in the logs — the same three facts as INC-00003. Database
CPU was up sharply relative to request rate, which INC-00003 did not do.

The separator was the trace. INC-00003 had **one long database span**. This had **sixty short
ones**. No metric distinguishes the two; the span tree does it instantly.

## Root cause

A refactor removed the `Include` from the order detail query. EF Core then loaded each order line
lazily, issuing one round trip per line.

## What made it hard

The previous incident. "Orders is slow with no errors" had a known answer, and the first hour was
spent confirming that answer rather than testing it. `get_slow_queries` sorted by mean time —
the sort that found INC-00003 — showed nothing, and that negative result was treated as "look
harder at the same thing" for half an hour before it was treated as evidence.

Sorting by call count instead took ten seconds.

## Fix

```csharp
await db.Orders
    .Include(o => o.Lines)
    .FirstOrDefaultAsync(o => o.Id == id, cancellationToken);
```

## Follow-up

An integration test now asserts the query count for the endpoints that load aggregates. It fails
the moment an `Include` is removed rather than under load a month later.

The other lesson is about investigation rather than code: a previous incident with the same
symptom is a hypothesis, not an answer. See INC-00003 for the one this was confused with.
