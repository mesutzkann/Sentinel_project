---
type: runbook
title: Runbook — connection pool exhaustion
service: orders
id: RB-001
scenario: DB_CONNECTION_POOL_EXHAUSTION
severity: high
---

# Runbook — connection pool exhaustion

## Symptom

Latency climbs to the client timeout ceiling and stays there. Error rate rises, and the errors
are timeouts rather than exceptions from the database. Throughput stays flat: the service is not
handling more work, it is handling the same work more slowly.

Logs carry a burst of:

```text
Npgsql.NpgsqlException: The connection pool has been exhausted, either raise
MaxPoolSize (currently 20) or Timeout (currently 15 seconds)
```

## Confirm it

1. `get_connection_count` — active connections pinned exactly at the pool maximum, not
   fluctuating below it. Pinned at a round number is the tell.
2. `get_response_time` — p99 sitting at the timeout value rather than merely elevated.
3. A trace: the time is spent **before** the database span begins. Connection acquisition is not
   instrumented as a span, so pool waiting shows up as a gap, not as a slow query.

## Rule out the lookalikes

| Also looks like | Distinguished by |
|---|---|
| Circuit breaker stuck open | Those failures are **fast**. Pool exhaustion is slow. |
| Slow query | The DB span itself is slow there; here it is normal once it starts. |
| Downstream latency cascade | Three services degrade there; here only the one with the pool. |

If the database reports plenty of headroom against `max_connections` while the service is
starved, that is the confirmation rather than a contradiction: the pool is per process and the
server limit is per instance.

## Fix

Restore `MaxPoolSize` in the connection string and restart the service:

```bash
update_env_and_restart orders ConnectionStrings__Postgres "...;MaxPoolSize=200"
```

Raising the pool is the right fix when the pool was lowered by mistake. It is the wrong fix when
connections are being held too long by a slow query or a leaked context — raising the ceiling
buys time and hides the cause. Check whether the query duration changed before deciding.

## Verify

Error rate back to zero within two minutes, p99 back under 100 ms, and active connections
fluctuating well below the maximum rather than pinned at it.

## Prevent

Alert on active connections above 80% of `MaxPoolSize` for five minutes. It fires before the
errors do, which is the entire point.
