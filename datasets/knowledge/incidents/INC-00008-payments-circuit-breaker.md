---
type: postmortem
title: INC-00008 — payments failing fast with the breaker stuck open
service: payments
id: INC-00008
scenario: CIRCUIT_BREAKER_STUCK_OPEN
severity: critical
date: 2026-06-28
duration_minutes: 71
resolution: restart_and_config_change
---

# INC-00008 — payments failing fast with the breaker stuck open

## Summary

Payments rejected 100% of authorisations for 71 minutes. The circuit breaker in front of the
payment gateway opened during a 40 second blip in the upstream provider and never closed again,
because its half-open probe used a path that could not succeed.

## Timeline

| Time | Event |
|---|---|
| 20:02 | Upstream provider returns 503 for about 40 seconds. |
| 20:03 | Breaker opens after 5 consecutive failures. |
| 20:03 | Upstream recovers. The breaker does not. |
| 20:06 | Paged. Error rate 100%, **p99 latency down to 4 ms**. |
| 20:25 | The low latency is noticed and read correctly: nothing is being attempted. |
| 20:44 | `Circuit breaker opened for payment-gateway` found in the logs, with its original cause. |
| 21:13 | Payments restarted, breaker thresholds widened. |

## What we saw

High error rate **and very low latency at the same time** — 4 ms p99 against a normal 25 ms.
Requests were failing without doing any work.

```text
Circuit breaker opened for payment-gateway after 5 consecutive failures
Polly.CircuitBreaker.BrokenCircuitException: The circuit is now open and is not allowing calls
```

Traces ended in single-digit milliseconds with an error status and no child spans at all.

## Root cause

Two things together: the breaker opened on a brief upstream blip that had already passed, and
its half-open probe called an endpoint that returns 503 by design when no session is
established. The probe could never succeed, so the breaker could never close.

## What made it hard

Twenty minutes went into looking for something slow. The instinct on a high error rate is to
look for saturation, and every latency number here was *better* than normal. Latency falling
while errors rise is the signature of failing fast, and it is the one thing that separates this
from connection pool exhaustion, where the failures are slow.

## Fix

Restarted payments to reset breaker state, then widened the threshold from 5 consecutive
failures to 20 in 30 seconds and pointed the half-open probe at an endpoint that can actually
return 200.

## Follow-up

Alert on the breaker's own state rather than on the error rate it produces. A breaker that has
been open for more than two minutes is always worth a page.

See also: [Runbook — circuit breaker stuck open](../runbooks/circuit-breaker-stuck-open.md),
and INC-00001 for the slow-failure failure it is most often confused with.
