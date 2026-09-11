"""What an investigation looks like on the wire, in the shape the backend's tables want.

The callback contract in docs/planning.md §3.2 carries a free-form ``payload`` per event, and
§4.1 says what the rows built from it hold. This module is the one place that translation is
written, for two reasons.

**The final payload has to stand alone.** ADR-0002 accepts that an intermediate event can be
lost — the backend restarting mid-investigation costs a timeline entry. It only accepts that
because the terminal event carries the whole investigation, so every row the backend needs can
still be written from it. That guarantee is only as good as this module: a field that exists in
an ``evidence_found`` event and not in :func:`final_payload` is a field that silently disappears
whenever delivery hiccups.

**The same fact should not be described twice.** ``VALIDATE`` emits a root cause event and the
final payload repeats the root cause; when those two were built in two places they were free to
disagree about, say, whether an unnamed category travels as ``null`` or as ``UNKNOWN``. They are
now the same function, so they cannot.

Serialising is deliberately dumb — dictionaries and primitives, no Pydantic model. What crosses
this boundary is JSONB on the other side, and a schema here would be a third place to keep in
step with the backend's C# records for no checking that the backend does not already do.
"""

from __future__ import annotations

from typing import Any

from agents.categories import category_or_unknown
from agents.context import (
    EvidenceItem,
    Hypothesis,
    InvestigationContext,
    Recommendation,
    RootCause,
)
from agents.states import State


def evidence_payload(item: EvidenceItem, index: int) -> dict[str, Any]:
    """One fact, as an ``evidence`` row.

    ``index`` is the position in ``ctx.evidence`` and is what every citation refers to — a
    hypothesis cites facts by number, so a payload without it cannot be joined back to the
    conclusion that rests on it.
    """
    return {
        "index": index,
        "source": item.source.value,
        "summary": item.summary,
        "weight": item.weight,
        "tool": item.tool,
        "raw": item.raw,
    }


def hypothesis_payload(hypothesis: Hypothesis) -> dict[str, Any]:
    """One candidate explanation, ranked or not.

    Both numbers travel: ``stated_confidence`` is what the model claimed and ``score`` is what
    ranking computed over cited evidence. The gap between them is the most diagnostic thing on
    the hypotheses panel, and averaging them into one number here would hide it.
    """
    return {
        "title": hypothesis.title,
        "description": hypothesis.description,
        "category": hypothesis.category,
        "stated_confidence": hypothesis.stated_confidence,
        "score": hypothesis.score,
        "rank": hypothesis.rank,
        "selected": hypothesis.selected,
        "evidence": list(hypothesis.supporting_evidence),
    }


def root_cause_payload(root_cause: RootCause) -> dict[str, Any]:
    """The conclusion, with the score and the critic's verdict attached.

    ``category`` goes out as ``UNKNOWN`` rather than ``null`` because the backend column requires
    one of the fifteen codes; see :mod:`agents.categories` for why that is not the same as
    guessing the nearest.
    """
    return {
        "title": root_cause.title,
        "category": category_or_unknown(root_cause.category),
        "explanation": root_cause.explanation,
        "evidence": list(root_cause.supporting_evidence),
        "hypothesis": root_cause.hypothesis_title,
        "confidence": root_cause.confidence,
        "confidence_breakdown": root_cause.confidence_breakdown,
        "validator_confidence": root_cause.validator_confidence,
        "validator_output": root_cause.validator_output,
    }


def recommendation_payload(recommendation: Recommendation) -> dict[str, Any]:
    """One proposed action. ``requires_approval`` is on the wire even though it is always True.

    Phase 10 is what makes these executable, and the backend stores them as
    ``pending_approval``. Sending the flag explicitly means the row is written from what the
    agent said rather than from what the backend assumes the agent meant.
    """
    return {
        "action_code": recommendation.action_code,
        "description": recommendation.description,
        "tool_name": recommendation.tool_name,
        "tool_args": recommendation.tool_args,
        "requires_approval": recommendation.requires_approval,
    }


def final_payload(
    ctx: InvestigationContext,
    *,
    final_state: State,
    transitions: int | None = None,
    duration_ms: int | None = None,
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Everything one run concluded, for the terminal event.

    This is the payload ADR-0002 leans on: whatever happened to the events before it, a backend
    that receives this one can write the investigation, its evidence, its hypotheses, its root
    cause and its recommendations. The only thing it cannot reconstruct is the timeline, which is
    the cost that ADR accepted.

    ``status`` is the backend's ``investigations.status`` vocabulary rather than the agent's
    state names: ``NEEDS_HUMAN`` is a terminal state here and there is no such status there, and
    translating it in the backend would mean two services having an opinion about what a run that
    would not stand behind its conclusion counts as. It counts as completed — it finished, and it
    has a result a human is being asked to look at.
    """
    return {
        "investigation_id": ctx.investigation_id,
        "incident_code": ctx.incident_code,
        "final_state": final_state.value,
        "status": "failed" if final_state is State.FAILED else "completed",
        "needs_human": final_state is State.NEEDS_HUMAN,
        "failure_reason": failure_reason,
        "transitions": transitions,
        "duration_ms": duration_ms,
        "query": ctx.query,
        "router_intent": ctx.route.intent.value if ctx.route is not None else None,
        "router_output": ctx.route.model_dump(mode="json") if ctx.route is not None else None,
        "target_service": ctx.target_service,
        "evidence": [evidence_payload(item, index) for index, item in enumerate(ctx.evidence)],
        "hypotheses": [hypothesis_payload(h) for h in ctx.hypotheses],
        "root_cause": root_cause_payload(ctx.root_cause) if ctx.root_cause is not None else None,
        "recommendations": [recommendation_payload(r) for r in ctx.recommendations],
        "notes": list(ctx.notes),
        "usage": {
            "llm_calls": ctx.llm_calls,
            "prompt_tokens": ctx.prompt_tokens,
            "completion_tokens": ctx.completion_tokens,
            "tool_calls": ctx.tool_calls_made,
            "tool_budget": ctx.tool_budget,
            "iterations": ctx.iteration,
        },
    }
