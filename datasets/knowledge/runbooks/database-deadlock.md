---
type: runbook
title: Runbook — database deadlock
service: payments
id: RB-003
scenario: DB_DEADLOCK
severity: high
---

# Runbook — database deadlock

## Symptom

Error rate in a **sawtooth**: it spikes, recovers on its own, spikes again. Never sustained,
never zero. Latency is elevated during the spikes only.

Logs carry PostgreSQL's own message, with the victim transaction's stack:

```text
Npgsql.PostgresException (0x80004005): 40P01: deadlock detected
DETAIL: Process 1834 waits for ShareLock on transaction 9922; blocked by process 1811.
```

`40P01` is the SQLSTATE for a deadlock and it is worth searching logs for directly.

## Confirm it

1. `get_locks_and_deadlocks` — blocked and blocking process pairs on the same two relations.
2. `get_recent_errors` — the `40P01` bursts line up with the error-rate spikes.
3. Failed traces cluster on one endpoint, and the same two tables appear in both directions.

PostgreSQL resolves a deadlock by killing one transaction, which is why the service recovers
without intervention. Self-recovery is the signature, not a sign that the problem went away.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Lock contention without deadlock | No `40P01`; queries are slow but succeed. |
| Circuit breaker | Sustained failure, not intermittent, and no database involvement. |
| Retry storm | The database is fine there; the load is the problem. |

## Fix

Lock ordering, not retries. Both transactions must acquire the two rows in the same order —
usually by sorting the identifiers before the updates:

```csharp
foreach (var id in ids.OrderBy(id => id))
{
    // ...
}
```

A retry-on-deadlock policy makes the symptom smaller and leaves the cause in place. It is a
reasonable belt-and-braces addition *after* the ordering is fixed, and a bad substitute for it.

## Verify

No `40P01` in the logs for ten minutes under the same load, and `get_locks_and_deadlocks`
returning no blocked pairs.

## Prevent

Keep transactions short and acquire rows in a documented order. `log_lock_waits` is already on,
so lock waits appear in the PostgreSQL log before they become deadlocks.
