---
type: postmortem
title: INC-00001 — orders timing out after a connection pool change
service: orders
id: INC-00001
scenario: DB_CONNECTION_POOL_EXHAUSTION
severity: high
date: 2026-03-02
duration_minutes: 47
resolution: config_change
---

# INC-00001 — orders timing out after a connection pool change

## Summary

For 47 minutes, roughly a third of checkouts failed with timeouts. The cause was a connection
string change that lowered Npgsql's `MaxPoolSize` from 200 to 20 as part of unrelated tuning
work. Orders could not acquire connections fast enough to serve its normal traffic.

## Timeline

| Time | Event |
|---|---|
| 09:14 | Configuration change deployed to orders. No visible effect for six minutes. |
| 09:20 | p99 latency crosses 5 s. Gateway error rate starts climbing. |
| 09:26 | Paged on gateway error rate. First look was at payments, which was healthy. |
| 09:41 | `get_connection_count` shows active connections pinned at exactly 20. |
| 09:52 | `MaxPoolSize` restored to 200, orders restarted. |
| 10:01 | Error rate zero, p99 back to 60 ms. |

## What we saw

Latency climbed to the client timeout ceiling and stayed there — flat at the ceiling rather than
rising smoothly. Throughput did not change; the same requests were simply taking far longer.
Logs carried bursts of `Npgsql.NpgsqlException: The connection pool has been exhausted`.

The trace was what settled it. Time was spent **before** the database span started, not inside
it. The queries themselves were as fast as ever once they got a connection.

## Root cause

`MaxPoolSize=20` in the orders connection string. At about 40 requests per second with a mean
query time of 15 ms, 20 connections cannot cover the concurrency, so requests queued for a
connection until the client timeout fired.

## What made it hard

Twelve minutes were spent looking at payments because the gateway was where the errors surfaced.
The gateway is the best detector of a problem in this estate and the worst locator of one.

The database also looked healthy throughout — `max_connections` is 300 for the instance and
nothing was near it. That is not a contradiction: the pool is per process, and a starved service
next to a comfortable database is the signature of a pool problem, not evidence against one.

## Fix

Restored `MaxPoolSize=200` and restarted. Verified with active connections fluctuating in the
30–60 range instead of pinned at the ceiling.

## Follow-up

Added an alert on active connections above 80% of `MaxPoolSize` for five minutes. It would have
fired at 09:18, eight minutes before the page.

See also: [Runbook — connection pool exhaustion](../runbooks/connection-pool-exhaustion.md).
