---
type: runbook
title: Runbook — CPU saturation
service: orders
id: RB-008
scenario: CPU_SATURATION
severity: medium
---

# Runbook — CPU saturation

## Symptom

CPU pinned above 90%. Latency rises with load rather than being flat-and-high. Error rate stays
flat until the service saturates, at which point requests start timing out at the edge. Nothing
in the logs.

## Confirm it

1. `get_cpu_usage` — `process_cpu_utilization` at or near 1.0 for the service, while the others
   are normal.
2. `get_container_stats` — CPU confirmed at the container level, memory normal. A leak drives
   both up; this drives only one.
3. `get_slowest_spans` — time is inside an **internal compute span**, with no database span and
   no HTTP span in the request. That absence is the diagnosis: the work is not I/O.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Missing index | Time is in a DB span there, and PostgreSQL's CPU rises, not the service's. |
| Memory leak | Memory climbs there; here it is flat. |
| Traffic increase | Request rate rises too, and CPU per request stays constant. |

CPU **per request** is the number that separates "more work arrived" from "each request became
more expensive". Divide utilisation by request rate before concluding anything.

## Fix

`apply_patch` — remove the hot loop from the request path, or cache its result. A hashing or
serialisation loop that runs per request and computes the same thing every time is the usual
shape.

Scaling out is a mitigation, not a fix: it multiplies the cost of the same wasted work.

## Verify

CPU back under 40% at the same request rate, and latency flat as load rises rather than climbing
with it.

## Prevent

Alert on CPU per request rather than raw utilisation, so a genuine traffic increase does not page
anyone and a regression in cost per request does.
