---
type: postmortem
title: INC-00002 — gateway failing while payments was healthy
service: gateway
id: INC-00002
scenario: TIMEOUT_TOO_LOW
severity: high
date: 2026-03-19
duration_minutes: 62
resolution: config_change
---

# INC-00002 — gateway failing while payments was healthy

## Summary

For just over an hour, about 70% of checkouts returned 502 from the gateway. Payments served
every one of those requests successfully. A configuration change had set the gateway's
`HttpClient.Timeout` to 500 ms against a dependency whose honest p99 is roughly 800 ms.

## Timeline

| Time | Event |
|---|---|
| 14:02 | Gateway configuration deployed with a 500 ms client timeout. |
| 14:03 | Gateway error rate steps from 0 to 68% at a single timestamp. |
| 14:07 | Paged. Payments dashboards checked first — all green, error rate zero. |
| 14:33 | A trace shows the gateway span erroring at 500 ms with the payments child span
completing at 780 ms. |
| 14:58 | Timeout restored to 30 s, gateway restarted. |
| 15:04 | Error rate zero. |

## What we saw

The gateway logged `TaskCanceledException: The request was canceled due to the configured
HttpClient.Timeout of 500 milliseconds elapsing`. Payments logged 200s for the same request ids
at the same moments. Two services disagreeing about whether the same request succeeded.

The onset was abrupt — a step from zero to 68% at one timestamp, not a ramp. Abrupt onset points
at a change, not at load.

## Root cause

The gateway's client timeout was below the real latency of what it calls. Every request slower
than 500 ms became an error at the edge even though it was served correctly downstream.

## What made it hard

Nothing in payments looked wrong, because nothing in payments **was** wrong. Half an hour went
into looking for a cause in the service that was working. The trace made it obvious in seconds
once it was opened: a parent span that errors while its child succeeds has exactly one class of
cause.

## Fix

Restored `HttpClient__Payments__TimeoutSeconds` to 30 and restarted the gateway.

## Follow-up

Client timeouts are now derived from the callee's p99 with headroom rather than picked as round
numbers. Added an alert for a caller's error rate rising while its callee's does not.

See also: [Runbook — client timeout set below real latency](../runbooks/client-timeout-too-low.md).
