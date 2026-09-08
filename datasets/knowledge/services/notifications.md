---
type: service_doc
title: notifications service
service: notifications
id: SVC-notifications
---

# notifications service

Port 8084, schema `svc_notifications`. Delivers notifications through a stub external gateway.
The only service in the estate with a dependency outside the estate.

## Endpoints

`GET /notifications`, `GET /notifications/{id}`, `POST /notifications`.

## Dependencies

- PostgreSQL, schema `svc_notifications`.
- `DeliveryGateway`, a stub SMTP and webhook target. It is an **external** span: it has no server
  side in the trace, so a failure there ends the trace at the service boundary rather than
  continuing into another service.

## How it fails

- **Memory climbs monotonically and never falls.** Working set rises, gen-2 collections rise with
  it, latency creeps up, and eventually `OutOfMemoryException`. Nothing in the logs until the
  very end. It is the only failure in the estate where a metric rises steadily over time rather
  than stepping to a new level, and that shape alone identifies it.
- **100% failure of this service while the database is provably healthy.** Every database-backed
  endpoint fails immediately with `Npgsql.NpgsqlException: Failed to connect`, health is
  degraded, and `get_database_health` reports the database as fine. A connection string pointing
  at a host that does not exist.
- **503s from an external host, with orders and payments completely clean.** The dependency is
  down; the failing span is outside the service boundary. Nothing in the estate is broken.

The first is slow and silent, the second instant and total. They share a service and nothing
else.

## Chaos scenarios owned

`MEMORY_LEAK` (7), `WRONG_CONNECTION_STRING` (9), `EXTERNAL_DEPENDENCY_UNAVAILABLE` (12).
