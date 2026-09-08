---
type: runbook
title: Runbook — client timeout set below real latency
service: gateway
id: RB-006
scenario: TIMEOUT_TOO_LOW
severity: high
---

# Runbook — client timeout set below real latency

## Symptom

One service's error rate is high while the service it calls has an error rate of **zero**. The
failing service logs `TaskCanceledException` or `The request was canceled due to the configured
HttpClient.Timeout of 500 milliseconds elapsing`; the called service logs successful 200s for the
very same requests.

## Confirm it

1. `get_trace_details` on a failed request — the parent span errors at exactly the timeout value,
   and its **child span completes successfully**, often after the parent has already given up.
   Errors landing on a suspiciously round number are the tell.
2. `get_container_logs` for both services, side by side. Caller fails, callee succeeds.
3. Compare the callee's honest p99 to the caller's configured timeout. If the timeout is below
   it, every slow-but-normal request fails.

Caller fails while callee succeeds is the clearest "look upstream" signal in the system. Nothing
else produces it.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Downstream latency cascade | The callee is genuinely slow there, and its own p99 rises. Here it is unchanged. |
| Circuit breaker open | No downstream call is made at all; there is no child span to succeed. |
| Pool exhaustion | The wait is on a connection, and it is the callee that reports errors. |

## Fix

Restore the timeout to a value above the callee's p99 with headroom:

```bash
update_env_and_restart gateway HttpClient__Payments__TimeoutSeconds 30
```

A timeout should be set from the measured latency of what it calls, not chosen as a round number.
Below p99 it converts normal slowness into errors; too far above it, it stops protecting anything.

## Verify

Gateway error rate to zero within a minute of the restart, with payments unchanged throughout —
it was never the problem.

## Prevent

Derive client timeouts from the callee's published latency objective, and alert when a caller's
error rate rises while its callee's does not. That divergence has exactly one cause family.
