---
type: postmortem
title: INC-00006 — intermittent payment failures from a deadlock
service: payments
id: INC-00006
scenario: DB_DEADLOCK
severity: high
date: 2026-05-30
duration_minutes: 95
resolution: code_fix
---

# INC-00006 — intermittent payment failures from a deadlock

## Summary

For an hour and a half, payment authorisation failed intermittently — spiking to 12%, recovering
to near zero, spiking again, in a sawtooth. Two code paths updated the same two rows in opposite
order under concurrency, and PostgreSQL was resolving the deadlocks by killing one side.

## Timeline

| Time | Event |
|---|---|
| 10:15 | Batch settlement job starts, overlapping with normal authorisation traffic. |
| 10:22 | First error spike to 12%, recovers within a minute. |
| 10:31 | Second spike. Paged after the third. |
| 10:44 | `get_recent_errors` shows `40P01: deadlock detected`. |
| 10:58 | `get_locks_and_deadlocks` shows blocked and blocking pairs on `payments` and `payment_events`. |
| 11:50 | Fix deployed: both paths now sort identifiers before updating. |

## What we saw

A sawtooth error rate — up, down, up — never sustained and never fully clear. Latency elevated
only during the spikes. Failed traces clustered on the authorisation endpoint.

The log line was decisive and searchable:

```text
Npgsql.PostgresException (0x80004005): 40P01: deadlock detected
DETAIL: Process 1834 waits for ShareLock on transaction 9922; blocked by process 1811.
```

## Root cause

The settlement job locked `payment_events` then `payments`; the authorisation path locked
`payments` then `payment_events`. Under overlap, each held what the other needed.

## What made it hard

Self-recovery. Every spike resolved on its own within a minute, so the first two looked like
transient blips and the service appeared healthy between them. Intermittent-and-self-recovering
is a signature, not a reason to wait.

## Fix

Both paths now acquire rows in ascending identifier order. A deadlock retry policy was
*considered* and deliberately not used as the fix: it would have made the symptom smaller and
left the cause in place. It was added afterwards as a secondary safeguard.

## Follow-up

Lock ordering is documented for the payments schema. `log_lock_waits` is on, so waits appear in
the PostgreSQL log before they become deadlocks.

See also: [Runbook — database deadlock](../runbooks/database-deadlock.md).
