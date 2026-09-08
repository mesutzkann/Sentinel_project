---
type: postmortem
title: INC-00010 — three services degraded, one service at fault
service: payments
id: INC-00010
scenario: DOWNSTREAM_LATENCY_CASCADE
severity: high
date: 2026-08-02
duration_minutes: 56
resolution: config_change
---

# INC-00010 — three services degraded, one service at fault

## Summary

p99 latency rose simultaneously in gateway, orders and payments, and checkout began timing out at
the edge. Three services looked unhealthy; one was. A 3 second delay had been introduced in the
payment authorisation path and everything upstream was simply waiting for it.

## Timeline

| Time | Event |
|---|---|
| 15:12 | A configuration change adds a deliberate delay to the authorisation path. |
| 15:15 | p99 rises in three services at once. Gateway starts timing out. |
| 15:18 | Paged on gateway error rate. |
| 15:24 | Orders investigated first — it is the loudest, with the largest absolute latency rise. |
| 15:41 | `get_slowest_spans` isolates a single 3 s span in payments; parents are all wait. |
| 15:52 | `get_service_dependencies` confirms nothing calls payments except orders. |
| 16:08 | Delay removed, payments restarted. |
| 16:11 | All three services normal. |

## What we saw

Three services degrading in lockstep, which reads as an infrastructure problem — a shared
database, a network, a host. It was not. The database was fine and the other two services were
fine; they were blocked on a call.

The evidence that settled it was the span **self time**. Gateway and orders had almost no time
of their own: their spans were long, and nearly all of that length was one child span. Payments
had a span that was long on its own account.

## Root cause

A 3 second artificial delay in the payment authorisation path, added by a configuration change
and not removed.

## What made it hard

The instinct to start with the service showing the biggest symptom. Orders had the largest
absolute latency increase because it waits for payments *and* does its own work, so it looked
like the epicentre. Following the dependency graph downwards rather than following the loudest
signal is what shortened this — and the graph is acyclic, so "downwards" is always well defined.

A multi-service symptom with a single-service cause is common. Simultaneous degradation across
services that call each other is evidence of a chain, not of shared infrastructure.

## Fix

Removed the delay and restarted payments.

## Follow-up

Dashboards now show span self time alongside total duration, so "slow" and "waiting on something
slow" are distinguishable at a glance rather than after twenty minutes.

See also: INC-00002, where the caller failed and the callee was healthy — the mirror image of
this one.
