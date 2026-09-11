# ADR-0006: An investigation survives a backend that is not answering

- **Status:** Accepted
- **Date:** 2026-09-11
- **Deciders:** project owner

## Context

[ADR-0002](0002-callback-over-queue.md) chose HTTP callbacks over a message queue and accepted
one consequence explicitly: delivery is not durable. That was the right trade for a portfolio
project on a 16 GB laptop, and it left a question open — *what the AI service does when a POST
fails* — which ADR-0002 answered in half a sentence ("bounded retry") and which Phase 7.5 had to
answer in code.

The question matters more than it sounds. An investigation is a minute or more of local model
calls and MCP tool calls; on this hardware a single run is 73 seconds and six model calls. The
backend, meanwhile, is a `dotnet run` in another window that gets restarted while the agent is
mid-run, several times a day, by the person developing it. If a failed callback took the
investigation down, the most expensive work the system does would be the most fragile thing in
it.

There is a second failure that looks the same and is not: a callback that is *rejected* rather
than unanswered. A bad callback token returns 401 every time. Retrying it spends the
investigation's wall clock to arrive at the same refusal.

## Decision

The emitter (`ai-service/agents/events.py`) is what all of this lives in, and the state machine
knows nothing about it.

1. **Delivery never raises into the run.** Every failure ends in a log line and, where it can be
   retried later, a buffered envelope. No node can be interrupted by the backend.
2. **Sequence numbers are assigned when an event is emitted, not when it is delivered.** A gap in
   the sequence is how the backend knows something was lost. Numbering on delivery would close
   the gap and produce a timeline that looks complete and is not.
3. **Transient and permanent failures are told apart.** 5xx, 408, 425 and 429 are retried — three
   attempts with exponential backoff, five for the terminal event. Every other 4xx is dropped
   after one attempt, with an error log.
4. **Undelivered events are buffered and retried ahead of the next one**, so the backend sees the
   run in the order it happened. The buffer is bounded (500 envelopes, roughly ten runs' worth);
   when it overflows the *oldest* go, because the newest carry the conclusion.
5. **The terminal event carries the whole investigation** — evidence, hypotheses, root cause,
   recommendations, usage — as `agents.payloads.final_payload`. This is what makes the loss of an
   intermediate event survivable rather than merely tolerable: a backend that receives only the
   last event can still write every row it owns.
6. **The callback token travels in `X-Callback-Token`**, not in the `X-Internal-Token` the
   service-to-service API already uses. That one is a long-lived shared secret; this one is
   issued for a single investigation, and two lifetimes sharing a header name means rotating one
   quietly widens the other. The backend's events endpoint (Phase 7.6) reads this header.
7. **A run that fails before the machine starts still reports.** If Ollama is down or a
   dependency will not build, the service posts a `failed` event itself. Without it the backend
   would hold the investigation in `running` for ever, which on the frontend is a spinner that
   never stops rather than an error a person can act on.

## Consequences

**Positive**

- The backend can be restarted mid-investigation and the run finishes, with a complete result and
  a timeline missing only the events that fell in the gap.
- A misconfigured callback token costs one request per event instead of three, and says so in the
  log rather than in the wall clock.
- The final payload is a second, complete description of the run, which is also what
  `GET /investigations/{id}` serves — so the AI service can be inspected on its own, with no
  backend at all. That is how the agent was developed before Phase 7.6 existed.

**Negative**

- The buffer is in memory. If the AI service itself restarts, held events are gone; the backend
  will see a gap and no recovery is attempted. Making this durable is what a queue would have
  been for, and ADR-0002 already declined that.
- Retries make a step slower to report when the backend is struggling — up to about 3.5 seconds
  of backoff for a step event. The investigation is unaffected, but the timeline lags.
- A backend that accepts an event and fails to persist it is indistinguishable from success here.
  Delivery is acknowledged, not confirmed.

## Alternatives considered

- **Fire-and-forget, no retry.** Simplest, and it loses an event every time the backend blinks.
  Rejected because the events it loses most often are the ones during a restart, which is exactly
  when somebody is watching.
- **Retry for ever, in the background, after the run ends.** Correct for durability and wrong for
  a demo: the process would hold state for investigations nobody is waiting on any more, and the
  failure would be invisible rather than logged.
- **Persist the buffer to the AI service's own PostgreSQL schema.** Durable across a restart of
  this service, and a second store of investigation state that the backend already owns. The
  moment there are two, there are two answers to what an investigation concluded.
