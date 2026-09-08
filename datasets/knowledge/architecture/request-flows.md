---
type: architecture
title: Request flows and service dependencies
id: ARCH-002
---

# Request flows and service dependencies

## Who calls whom

```text
gateway ──→ users
        ──→ orders ──→ payments
        ──→ payments
        ──→ notifications
```

Nothing calls the gateway except the outside world, and nothing calls back upwards. The graph is
acyclic on purpose: a cycle would make "which service is the cause" ambiguous in a way that has
nothing to teach an investigation agent.

## Checkout, span by span

A successful `POST /api/checkout` produces one trace with these spans:

1. `POST /api/checkout` — gateway, root span.
2. `GET /users/{id}` — gateway client span, users server span, one DB span.
3. `POST /orders` — gateway client span, orders server span.
4. `INSERT orders` — orders DB span.
5. `POST /payments/authorize` — orders client span, payments server span.
6. `INSERT payments` — payments DB span.

Total is normally 40–80 ms locally. Payments is deliberately the slowest leg at roughly 25 ms,
because several scenarios are about latency that originates there and is felt at the edge.

## Reading a failure from the shape of the trace

The span tree usually names the cause on its own, and the four shapes are distinct:

| Shape | Reading |
|---|---|
| Parent span errors, child span **succeeds** | The caller gave up. A timeout on the caller's side, not a failure on the callee's. |
| Parent waits, one child span is long | The callee is slow. Follow it down; the parents are victims. |
| Time spent **before** the DB span starts | Waiting to acquire a connection, not waiting on the query. |
| Many sibling DB spans, each fast | An N+1 query. The total is large; no single query is. |

The third row is the one people get wrong. A trace where the database span itself is fast can
still be a database problem — the wait is in the pool, and the pool is not instrumented as a
span.

## Where retries live

The gateway does not retry. Orders retries its call to payments through a policy configured in
`Services:Payments`; users retries its call to orders. A retry multiplies downstream load without
changing upstream load, which is the only signal that separates an amplification problem from a
genuine traffic increase.
