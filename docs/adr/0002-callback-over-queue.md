# ADR-0002: HTTP callbacks between backend and AI service, not a message queue

- **Status:** Accepted
- **Date:** 2026-09-08
- **Deciders:** project owner

## Context

An investigation is long-running: the agent walks a state machine, calls MCP tools and invokes a
local LLM several times, so a single run takes anywhere from tens of seconds to several minutes.
The frontend has to show progress *while it happens* — a timeline that fills in step by step is
the most visible thing this project does.

That rules out a synchronous request/response between the ASP.NET Core backend and the Python AI
service. The question is what replaces it.

## Decision

The backend starts an investigation with `POST /investigations` and gets `202 Accepted`
immediately. The AI service then **POSTs one event per state transition** back to
`/internal/investigations/{id}/events` on the backend. The backend persists each event and
pushes it to the browser over SignalR.

Each investigation is issued a **single-use HMAC callback token** at start time; the backend
rejects events that do not carry it.

## Consequences

**Positive**

- No extra infrastructure. On a 16 GB machine, not running RabbitMQ or Redis is a real saving,
  and there is one less thing that can be broken during a demo.
- Every step is an ordinary HTTP request, so it shows up in traces and logs like everything else.
  The system that investigates incidents is itself observable — which is the Phase 11
  self-observability story.
- Events carry a monotonic `sequence` number, so the backend can detect gaps and the frontend
  can order steps deterministically even if delivery is out of order.

**Negative**

- Delivery is not durable. If the backend is down when the AI service posts an event, that event
  is lost. Mitigated with bounded retry and the fact that the AI service holds the full
  `InvestigationContext` and posts a complete final payload at the end — so a lost intermediate
  event costs a timeline entry, never the result.
- Both services must be reachable from each other. Fine inside one compose network; would need
  revisiting if they were ever deployed apart.

## Alternatives considered

- **RabbitMQ or Redis Streams** — durable and the right answer at production scale, rejected as
  infrastructure the portfolio does not need and the laptop would rather not run.
- **Server-Sent Events from AI service straight to browser** — cuts the backend out of the path,
  but then nothing persists the steps, and the backend has to stay the single source of truth.
- **Polling** — simplest of all, rejected because a timeline that updates on a 2-second poll
  looks worse in a demo than one that streams.
