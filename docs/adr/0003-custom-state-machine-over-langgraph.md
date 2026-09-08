# ADR-0003: A hand-written agent state machine, not LangGraph

- **Status:** Accepted
- **Date:** 2026-09-08
- **Deciders:** project owner

## Context

The investigation agent moves through explicit states — understand, plan, collect logs, collect
metrics, collect traces, check deployments, search history, inspect code, generate hypotheses,
rank, select root cause, validate, recommend — with a bounded ability to loop back when a
hypothesis needs more evidence.

LangGraph exists precisely for this and would work. The question is whether importing it serves
the project.

## Decision

Write the state machine ourselves: an `InvestigationContext` dataclass, a `Node` base class with
`run(ctx) -> Transition(next_state, events)`, and a small driver loop with iteration and tool-call
budgets. Roughly 300 lines.

## Consequences

**Positive**

- The flow is the deliverable. This is a portfolio project whose point is demonstrating that the
  author understands agent architecture; a graph assembled from someone else's primitives
  demonstrates that they can read a quickstart. Owning the loop means being able to answer
  "what happens when validation fails twice?" by pointing at code.
- Every transition is a natural event boundary, which is exactly what ADR-0002's callback stream
  and the frontend's investigation graph need. With a framework, we would be adapting its
  callback model to ours.
- Budgets (`max_iterations = 3`, 25 tool calls) and the confidence gate that routes to
  `NEEDS_HUMAN` below 0.70 are first-class in our own loop rather than bolted on.
- No dependency churn. LangChain-family packages move fast and break often; the AI service
  already carries torch, transformers and sentence-transformers, which is enough version risk.

**Negative**

- We reimplement things LangGraph gives away: checkpointing, resumption after a crash,
  visualisation. Checkpointing is not needed while a run fits in one process; if it ever is, the
  context is a plain serialisable object.
- No community-standard vocabulary. A reader who knows LangGraph has to learn our node contract
  instead — mitigated by keeping the contract to one method and documenting it in
  `docs/agent.md`.

**Neutral**

- Migration stays open. The node interface is deliberately close to LangGraph's, so wrapping each
  node as a LangGraph node later is mechanical.

## Alternatives considered

- **LangGraph** — the obvious choice for production, rejected for the reason above.
- **Plain sequential script, no state machine** — simpler, but loses the loop-back behaviour that
  makes the agent look like an investigator instead of a checklist, and gives the frontend graph
  nothing to draw.
- **Semantic Kernel** (keeps everything in .NET) — would remove the Python service entirely, but
  the RAG and fine-tuning stack this project is built to show off lives in Python.
