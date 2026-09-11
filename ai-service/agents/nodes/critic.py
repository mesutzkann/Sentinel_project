"""VALIDATE: a second pass whose job is to disagree, and the state where confidence is decided.

docs/planning.md §7 gives this state two rules. If the verdict is invalid, go back to
GENERATE_HYPOTHESES once; on the second rejection, stop at NEEDS_HUMAN. And if confidence is
below 0.70, stop at NEEDS_HUMAN and recommend nothing.

Both rules are here rather than spread across the nodes they concern, because both are the same
decision — whether this conclusion is one the system will stand behind — and a threshold checked
in the node that acts on it is a threshold that can be forgotten by the next node that acts on it.

**The critic sees all the evidence, not the cited subset.** Asking "do these three facts support
this claim" invites agreement, because they were chosen to. The interesting failure is a
conclusion that ignores the fact standing against it, and a critic shown only the citations
cannot find it.

**Confidence is computed here, not asked for.** The critic supplies one term of four — its own
confidence, a quarter of the score. The rest is arithmetic over what was collected and what won:
see :mod:`agents.confidence`, including why a term that could not be measured is dropped rather
than written down as zero.

**A rejected conclusion is discarded, not recorded.** On the first rejection the root cause is
cleared before the machine loops back, so an investigation ends with one root cause — the one it
settled on — rather than a trail of drafts. The critic's objections survive on
``ctx.critic_feedback``, which is what the next round of hypotheses is shown; that is the part
worth keeping, because a rejection nobody tells the model about produces the same answer again.
"""

from __future__ import annotations

import logging

from agents.confidence import (
    ConfidenceScore,
    evidence_support,
    hypothesis_margin,
    score_confidence,
)
from agents.context import InvestigationContext, RootCause
from agents.nodes.reasoning import ReasoningNode, render_evidence, render_incident
from agents.payloads import root_cause_payload
from agents.schemas import CriticVerdict
from agents.state_machine import AgentEvent, EventType, Transition
from agents.states import State
from llm.structured import StructuredOutputError

logger = logging.getLogger(__name__)

# The counter name on the context. A node that runs more than once cannot count its own runs, and
# "on the second failure, stop" is a rule about the run rather than about this instance.
FAILURE_COUNTER = "validate_failures"

# How many times a rejected conclusion is sent back for another attempt. One, from the planning
# document: a second rejection means the disagreement is about the evidence rather than about how
# it was phrased, and a third round would be the same two models failing to agree more slowly.
MAX_REJECTIONS = 1


class ValidateNode(ReasoningNode):
    """Checks the conclusion against the evidence, then scores it."""

    prompt_name = "critic"

    @property
    def state(self) -> State:
        return State.VALIDATE

    async def run(self, ctx: InvestigationContext) -> Transition:
        root_cause = ctx.root_cause

        if root_cause is None:
            reason = "there is no root cause to validate"
            ctx.note(reason)

            return Transition(next_state=State.NEEDS_HUMAN, message=reason)

        try:
            result = await self.ask(
                ctx,
                CriticVerdict,
                incident=render_incident(ctx),
                evidence=render_evidence(ctx),
                root_cause=(
                    f"{root_cause.title} [{root_cause.category or 'uncategorised'}]\n"
                    f"{root_cause.explanation}\n"
                    f"Cited evidence: {root_cause.supporting_evidence or 'none'}"
                ),
            )
        except StructuredOutputError as exc:
            return self.stalled(ctx, exc, producing="validation verdict")

        verdict = result.value
        root_cause.validator_output = verdict.model_dump()
        root_cause.validator_confidence = verdict.confidence

        if not verdict.valid:
            return self._rejected(ctx, root_cause, verdict, self.usage(result))

        score = self._score(ctx, root_cause, verdict)
        payload = {
            **self.usage(result),
            "valid": True,
            "validator_confidence": verdict.confidence,
            "concerns": verdict.concerns,
            "confidence": score.to_payload(),
        }

        if not score.meets_threshold:
            # Held back by the number rather than by the critic: the explanation may be sound and
            # still rest on too little. No recommendation follows, which is the point of the
            # threshold — an action taken on a 0.6 conclusion is the failure mode this whole
            # phase is built to avoid.
            reason = (
                f"the conclusion stands but is not certain enough to act on: {score.explain()}"
            )
            ctx.note(reason)

            return Transition(
                next_state=State.NEEDS_HUMAN,
                message=reason,
                events=(self._root_cause_event(root_cause, accepted=True),),
                payload={**payload, "below_threshold": True},
            )

        message = f"the critic accepts the conclusion; {score.explain()}"

        return Transition(
            next_state=State.RECOMMEND_FIX,
            message=message,
            events=(self._root_cause_event(root_cause, accepted=True),),
            payload=payload,
        )

    def _rejected(
        self,
        ctx: InvestigationContext,
        root_cause: RootCause,
        verdict: CriticVerdict,
        usage: dict[str, object],
    ) -> Transition:
        """Send the run back for another explanation, or stop if it has been back already."""
        failures = ctx.bump(FAILURE_COUNTER)
        objections = self._objections(verdict)
        ctx.critic_feedback.extend(objections)

        if failures > MAX_REJECTIONS:
            # Second rejection. The conclusion is kept this time, with its score, because a human
            # is now the audience and "here is what it concluded and why the critic disagreed" is
            # more use than an empty result.
            score = self._score(ctx, root_cause, verdict)
            reason = (
                f"the critic rejected the conclusion twice; stopping for a human. "
                f"{'; '.join(objections) or 'no reason given'}"
            )
            ctx.note(reason)

            return Transition(
                next_state=State.NEEDS_HUMAN,
                message=reason,
                events=(self._root_cause_event(root_cause, accepted=False),),
                payload={
                    **usage,
                    "valid": False,
                    "rejections": failures,
                    "concerns": verdict.concerns,
                    "unsupported_claims": verdict.unsupported_claims,
                    "confidence": score.to_payload(),
                },
            )

        ctx.root_cause = None
        reason = (
            f"the critic rejected '{root_cause.title}': "
            f"{'; '.join(objections) or 'no reason given'}"
        )
        ctx.note(reason)
        logger.info("VALIDATE: %s", reason)

        return Transition(
            next_state=State.GENERATE_HYPOTHESES,
            message=reason,
            payload={
                **usage,
                "valid": False,
                "rejections": failures,
                "concerns": verdict.concerns,
                "unsupported_claims": verdict.unsupported_claims,
                "alternative": verdict.alternative,
            },
        )

    @staticmethod
    def _score(
        ctx: InvestigationContext,
        root_cause: RootCause,
        verdict: CriticVerdict,
    ) -> ConfidenceScore:
        """Compute the confidence and write it onto the root cause.

        ``historical_similarity`` is passed as ``None`` and will stay that way until Phase 9 adds
        the incident similarity search. That is a missing measurement rather than a zero, and the
        breakdown says so wherever the number is displayed.
        """
        score = score_confidence(
            evidence_support=evidence_support(ctx.evidence, root_cause.supporting_evidence),
            validator_confidence=verdict.confidence,
            historical_similarity=None,
            hypothesis_margin=hypothesis_margin(ctx.hypotheses),
        )

        root_cause.confidence = round(score.value, 4)
        root_cause.confidence_breakdown = score.to_payload()

        return score

    def _root_cause_event(
        self,
        root_cause: RootCause,
        *,
        accepted: bool,
    ) -> AgentEvent:
        """The one root cause event of the run, emitted only once the score is known.

        Serialised by :func:`agents.payloads.root_cause_payload`, which is also what the terminal
        event carries. One function rather than two, because a conclusion that reached the
        backend twice in two shapes would be a conclusion the frontend could render two ways.
        """
        return AgentEvent(
            type=EventType.ROOT_CAUSE,
            state=self.state,
            message=root_cause.title,
            payload={**root_cause_payload(root_cause), "accepted_by_critic": accepted},
        )

    @staticmethod
    def _objections(verdict: CriticVerdict) -> list[str]:
        """The critic's reasons, as lines the next round of hypotheses will be shown."""
        objections = [*verdict.concerns, *verdict.unsupported_claims]

        if verdict.alternative:
            objections.append(f"a better explanation may be: {verdict.alternative}")

        return objections
