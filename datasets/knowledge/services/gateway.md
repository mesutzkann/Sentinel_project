---
type: service_doc
title: gateway service
service: gateway
id: SVC-gateway
---

# gateway service

The edge. Port 8080, no database, no schema. Everything it returns it got from somewhere else.

## Endpoints

| Endpoint | Calls |
|---|---|
| `POST /api/checkout` | users, then orders, which calls payments |
| `GET /api/orders/{id}` | orders |
| `GET /api/services/health` | all four, aggregated |

## Dependencies

`DownstreamClient` wraps one `HttpClient` per downstream service. The timeout is 30 seconds and
it is configuration rather than code, which is what makes scenario 8 (`TIMEOUT_TOO_LOW`) a real
misconfiguration rather than a simulated one.

The gateway does not retry. A downstream failure is returned, not absorbed.

## How it fails

Because it holds no state, almost every gateway failure is somebody else's failure seen from the
edge. The exception is the one worth knowing:

> **The gateway errors while the service it called succeeds.** Gateway logs show
> `TaskCanceledException`, payments logs show 200s, and in the trace the gateway span errors at
> exactly the timeout while its child span completes normally. The cause is the gateway's own
> timeout being shorter than the callee's honest latency. Look upstream, not down.

Its error rate is the best detector in the estate and the worst locator. Use it to know that
something is wrong; use the trace to know what.

## Chaos scenarios owned

- `TIMEOUT_TOO_LOW` (8) — `HttpClient.Timeout` dropped to 500 ms while payments legitimately
  takes about 800 ms.
