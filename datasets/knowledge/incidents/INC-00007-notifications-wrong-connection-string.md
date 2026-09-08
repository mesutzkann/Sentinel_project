---
type: postmortem
title: INC-00007 — notifications down completely after an environment change
service: notifications
id: INC-00007
scenario: WRONG_CONNECTION_STRING
severity: critical
date: 2026-06-14
duration_minutes: 23
resolution: config_change
---

# INC-00007 — notifications down completely after an environment change

## Summary

Notifications failed 100% of database-backed requests for 23 minutes after an environment
variable change pointed its connection string at a host that does not exist. The database itself
was healthy throughout, and every other service was unaffected.

## Timeline

| Time | Event |
|---|---|
| 08:31 | Environment update sets `POSTGRES_HOST` to `postgres-primary` — a name from a different environment. |
| 08:31 | Notifications restarts, fails every request immediately. Health degraded. |
| 08:33 | Paged on the health endpoint, not on error rate. |
| 08:38 | `get_container_logs` shows `Npgsql.NpgsqlException: Failed to connect` on every request. |
| 08:41 | `get_database_health` reports the database healthy, accepting connections, well under `max_connections`. |
| 08:49 | Host restored, service restarted. |
| 08:54 | Fully recovered. |

## What we saw

Total, immediate, consistent failure of one service. Not a percentage — every single
database-backed request, from the first one after restart. The failures were instant: no
timeouts, no slow degradation.

```text
Npgsql.NpgsqlException: Failed to connect to postgres-primary:5432
---> System.Net.Sockets.SocketException: Name or service not known
```

Meanwhile orders, payments and users were completely clean against the same database.

## Root cause

`POSTGRES_HOST` was set to a hostname that does not resolve inside the compose network.

## What made it hard

It was not hard, and that is the useful part. The combination of 100% failure of exactly one
service and a database that is provably healthy is almost unambiguous: it is the service's view
of the database that is wrong, not the database.

The trap to avoid is starting a database investigation because the error message says
`NpgsqlException`. The exception names the client library, not the server.

## Fix

Restored the correct host and restarted.

## Follow-up

Health checks now fail fast and loudly on a connection failure at startup rather than accepting
traffic and failing per request. Environment values that name hosts are validated at boot.

See also: INC-00001, where the same library raised a different exception for a completely
different reason.
