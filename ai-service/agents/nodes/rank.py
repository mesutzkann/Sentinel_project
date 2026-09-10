"""RANK_HYPOTHESES: choosing between explanations with arithmetic instead of a second opinion.

There is an obvious alternative — ask the model which of its own hypotheses is best — and it is
the wrong one twice over. It would be the same model grading the answer it just wrote, and it
would put the decisive step of the investigation somewhere no one can inspect. Ranking here is a
formula over two numbers, both of which are already on the screen.

    score = 0.65 * evidence_support + 0.35 * stated_confidence

``evidence_support`` is the same function the confidence score uses (:mod:`agents.confidence`):
how much of the collected evidence a hypothesis cites, weighted, with a bonus for citing across
independent kinds of signal. ``stated_confidence`` is what the model said about its own
hypothesis relative to the others.

**Why the model's opinion is in there at all, at a third.** Two hypotheses can cite exactly the
same facts and be very different explanations of them — "the pool was exhausted" and "somebody
restarted the database" both rest on the connection count. Evidence support cannot separate
those; plausibility can, and plausibility is the one thing a language model is genuinely
contributing here. It is a third rather than a half so that it can break a tie and cannot
overturn a difference in evidence: a hypothesis that cites nothing and calls itself certain
scores 0.35 and loses to one at 0.6 support that called itself a coin flip.

Ties keep the model's original order, which is its own plausibility ordering, so the ranking is
stable and reproducible run to run.
"""

from __future__ import annotations

from agents.confidence import evidence_support
from agents.context import InvestigationContext
from agents.state_machine import AgentEvent, EventType, Node, Transition
from agents.states import State

# The split between what the evidence supports and what the model believes. See the module
# docstring: the majority share belongs to the measurable term.
SCORE_EVIDENCE_SHARE = 0.65
SCORE_MODEL_SHARE = 0.35


class RankHypothesesNode(Node):
    """Scores every hypothesis against the evidence and orders them."""

    @property
    def state(self) -> State:
        return State.RANK_HYPOTHESES

    async def run(self, ctx: InvestigationContext) -> Transition:
        if not ctx.hypotheses:
            # Reachable only if a future node clears the list; GENERATE_HYPOTHESES either
            # produces at least one or ends the run itself.
            reason = "there are no hypotheses to rank"
            ctx.note(reason)

            return Transition(next_state=State.NEEDS_HUMAN, message=reason)

        for hypothesis in ctx.hypotheses:
            support = evidence_support(ctx.evidence, hypothesis.supporting_evidence)
            hypothesis.score = round(
                SCORE_EVIDENCE_SHARE * support + SCORE_MODEL_SHARE * hypothesis.stated_confidence,
                4,
            )

        # `sorted` is stable, so equal scores keep the order the model proposed them in.
        ctx.hypotheses = sorted(ctx.hypotheses, key=lambda h: h.score, reverse=True)

        events: list[AgentEvent] = []

        for rank, hypothesis in enumerate(ctx.hypotheses, start=1):
            hypothesis.rank = rank
            hypothesis.selected = False
            events.append(
                AgentEvent(
                    type=EventType.HYPOTHESIS,
                    state=self.state,
                    message=f"{rank}. {hypothesis.title} ({hypothesis.score:.2f})",
                    payload={
                        "title": hypothesis.title,
                        "description": hypothesis.description,
                        "category": hypothesis.category,
                        "score": hypothesis.score,
                        "rank": rank,
                        "stated_confidence": hypothesis.stated_confidence,
                        "evidence": hypothesis.supporting_evidence,
                    },
                )
            )

        # The hypothesis events are emitted here and nowhere else. GENERATE_HYPOTHESES could
        # announce them a step earlier, but it would announce them without a score or a rank, and
        # the backend would either store a row it has to update or store two rows for one idea.
        top = ctx.hypotheses[0]
        margin = (
            top.score - ctx.hypotheses[1].score if len(ctx.hypotheses) > 1 else None
        )

        message = (
            f"{len(ctx.hypotheses)} ranked; '{top.title}' leads at {top.score:.2f}"
            + (f", by {margin:.2f}" if margin is not None else " (no runner-up to compare with)")
        )

        return Transition(
            next_state=State.SELECT_ROOT_CAUSE,
            message=message,
            events=tuple(events),
            payload={
                "ranked": [
                    {"rank": h.rank, "title": h.title, "score": h.score} for h in ctx.hypotheses
                ],
                "margin": None if margin is None else round(margin, 4),
                "weights": {
                    "evidence_support": SCORE_EVIDENCE_SHARE,
                    "stated_confidence": SCORE_MODEL_SHARE,
                },
            },
        )
