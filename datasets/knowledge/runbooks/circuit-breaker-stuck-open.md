---
type: runbook
title: Runbook — circuit breaker stuck open
service: payments
id: RB-007
scenario: CIRCUIT_BREAKER_STUCK_OPEN
severity: critical
---

# Runbook — circuit breaker stuck open

## Symptom

High error rate **and very low latency at the same time**. Requests fail in single-digit
milliseconds because no work is being done: the breaker is rejecting them before the call is
attempted.

Logs show the transition, then a steady stream of rejections:

```text
Circuit breaker opened for payment-gateway after 5 consecutive failures
Polly.CircuitBreaker.BrokenCircuitException: The circuit is now open and is not allowing calls
```

## Confirm it

1. `get_error_rate` and `get_response_time` together. Errors up, latency **down**. That
   combination is unique to this failure in this system.
2. `search_logs` for `BrokenCircuitException` and for the `opened for` line, which carries the
   original reason the breaker tripped.
3. Traces: spans ending in a few milliseconds with an error status and no child spans.

Latency dropping while errors rise is counter-intuitive and diagnostic. Failing fast is cheaper
than succeeding.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Pool exhaustion | Slow failures there, fast ones here. This is the discriminator. |
| External dependency down | The call is actually made there and fails at the boundary; here it is never made. |
| Timeout too low | The callee succeeds there. Here nothing is called. |

## Fix

Find out why it opened before reopening it. The `opened for` log line names the original
failures, and if that cause is still present the breaker will trip again within seconds.

When the cause is resolved:

1. `restart_container payments` — resets breaker state.
2. Tune the thresholds if the breaker opened on a transient blip: a 5-failure threshold with a
   30 second break is aggressive for a dependency that fails occasionally by design.

## Verify

Error rate to zero and latency back **up** to its normal 25 ms. Latency rising is the success
signal here, which is worth saying out loud because every other runbook wants the opposite.

## Prevent

Half-open probes must be able to succeed: if the probe uses the same path that is failing, the
breaker can never close. Alert on the breaker's own state rather than on the error rate it
produces.
