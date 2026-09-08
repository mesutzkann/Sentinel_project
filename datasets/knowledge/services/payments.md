---
type: service_doc
title: payments service
service: payments
id: SVC-payments
---

# payments service

Port 8083, schema `svc_payments`. Authorises payments for orders. Deliberately the slowest leg
of checkout at roughly 25 ms, because several failures are about latency that starts here and is
felt at the edge.

## Endpoints

`GET /payments`, `GET /payments/{id}`, `POST /payments/authorize`.

## Dependencies

PostgreSQL, schema `svc_payments`. A stub payment gateway behind a circuit breaker.

## How it fails

- **High error rate with very low latency.** Requests fail without doing work: the breaker is
  open and its half-open probe never succeeds. Spans end in single-digit milliseconds with an
  error status. Low latency plus high errors is unique to this failure, and it is what separates
  it from pool exhaustion, where the failures are slow.
- **`NullReferenceException` on a subset of requests, latency unchanged.** A currency the mapping
  table does not have, dereferenced without a guard. Fast failures, not all requests, no latency
  signature at all.
- **Deadlocks.** `40P01: deadlock detected`, and a sawtooth error rate: it spikes, recovers,
  spikes again. Never sustained, which is the signature — two transactions taking the same two
  rows in opposite order.
- **p99 rises here and in two other services at once.** Payments is slow; orders and gateway are
  waiting on it. `get_slowest_spans` isolates a single slow span in payments while its parents
  merely wait. A multi-service symptom with a single-service cause.

## Chaos scenarios owned

`DB_DEADLOCK` (3), `NULL_REFERENCE_EXCEPTION` (5), `DOWNSTREAM_LATENCY_CASCADE` (11),
`CIRCUIT_BREAKER_STUCK_OPEN` (13), `BAD_DEPLOYMENT_REGRESSION` (14).
