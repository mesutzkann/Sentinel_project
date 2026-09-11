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

**Why the prompt is on v2, and what v1 got wrong.** The benchmark over the five implemented
scenarios found the critic, not the model size, to be what stood between a correct root cause and
a finished investigation: every run of both the 3B and the 7B ended in NEEDS_HUMAN, and the 3B
threw out an already-correct conclusion in six of its ten rejections. Three things were wrong
with asking it that way, and v2 changes all three.

*It asked for fault-finding and got fault-finding.* "Your job is to find what is wrong with it",
followed by four questions each shaped as a way to disagree. A 3B follows the dominant
instruction. v2 asks one question instead — does the evidence support this cause better than any
other explanation of it — and lists the reasons to reject as a closed set.

*It never separated a wrong diagnosis from an overreaching sentence.* Every causal chain has a
link that is inferred rather than shown, so "is any link asserted rather than shown" is always
yes, and a model that has just written down a concern is not going to answer "valid" next. v2
says so explicitly: concerns and unsupported claims are expected on a conclusion it accepts, and
what they lower is the confidence, not the verdict.

*It decided first and looked afterwards.* ``valid`` was the first field of the schema, and
constrained decoding fills fields in order. :class:`agents.schemas.CriticVerdict` now asks for
the supporting and contradicting evidence indices first, so the verdict is the last thing
generated and has something to follow from — and both lists end up on the screen, where a human
can check the critic's reading the same way the critic checks the conclusion's.

**What all of that is worth, measured.** Same five scenarios, same model, before and after: the
same three correct root causes, but **none of the five finished before and two do now**, six
recommendations where there were none, one rejection where there were ten, and 4.0 model calls
per investigation instead of 6.0. The runs that concluded wrongly still stop — 0.66 and 0.33,
below the threshold — and so does a correct one the critic doubted at 0.62. See
[ADR-0007](../../../docs/adr/0007-critic-veto-needs-grounds.md), and
``python -m evaluation.reasoning_eval`` to reproduce it.
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

    # v1 is kept because it is what the 3B and 7B numbers in docs were measured against, and it
    # is the thing v2 has to beat. See the module docstring for what it got wrong.
    default_prompt_version = "v2"

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
        overruled = not verdict.valid and not self._grounds(verdict)

        if not verdict.valid and not overruled:
            return self._rejected(ctx, root_cause, verdict, self.usage(result))

        if overruled:
            reason = (
                "the critic voted against the conclusion without naming a reason its own rules "
                f"accept — it cites {verdict.supporting_evidence} as supporting it, nothing as "
                "contradicting it, and offers no better explanation; the objection is kept as a "
                "concern and lowers the confidence instead of ending the round"
            )
            ctx.note(reason)
            logger.info("VALIDATE: %s", reason)

        score = self._score(ctx, root_cause, verdict)
        payload = {
            **self.usage(result),
            "valid": True,
            "verdict_overruled": overruled,
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

    @staticmethod
    def _grounds(verdict: CriticVerdict) -> bool:
        """Whether a rejection rests on one of the three reasons the prompt allows.

        The critic may overturn a conclusion because nothing collected supports it, because
        something collected contradicts it, or because it can name an explanation that fits the
        same facts better. Those are the three, and the schema makes each of them a field, so the
        verdict can be checked against its own grounds rather than taken on the model's word.

        **A 3B needs that check.** Prompt v2 halved the rejections and the ones left over were a
        model voting against a conclusion while filling in "supported by [0, 1, 5], contradicted
        by nothing" directly above the vote — a correct deadlock diagnosis, thrown out twice, on
        the grounds that the evidence did not *prove* it. A veto nobody can point at is an
        opinion, and this system already has a place for a model's opinion about how strong the
        evidence is: it is a quarter of the confidence score, and the 0.70 threshold is the other
        gate. Making the veto need a reason does not disarm the critic — it moves an unarguable
        objection from "end the round" to "lower the number", where it can still stop the run.
        """
        return (
            not verdict.supporting_evidence
            or bool(verdict.contradicting_evidence)
            or bool(verdict.alternative)
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
        ctx.critic_feedback.append(self._feedback_line(root_cause.title, objections))

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
    def _feedback_line(title: str, objections: list[str]) -> str:
        """One rejection as one line: what was rejected, and why.

        The hypotheses prompt introduces this list as "rejected in an earlier round — do not
        propose these again", and until now it was handed the *objections* instead: lines like
        "the connection count is for the whole estate". A small model reads those as the things
        not to propose and walks away from the very explanation the objection was about, which is
        what the second round of a real run looked like — a correct database diagnosis in round
        one, and failed traces in round two. Naming the conclusion makes the list say what the
        heading promises.
        """
        reasons = "; ".join(objections) or "no reason given"

        return f'"{title}" was rejected: {reasons}'

    @staticmethod
    def _objections(verdict: CriticVerdict) -> list[str]:
        """The critic's reasons, as lines the next round of hypotheses will be shown.

        The contradicting indices lead, because they are the specific thing the next round has to
        account for: "evidence [3] contradicts this" points at a fact, where "the evidence is
        insufficient" points at nothing and produces the same hypothesis again with softer
        wording.
        """
        objections: list[str] = []

        if verdict.contradicting_evidence:
            cited = ", ".join(f"[{index}]" for index in verdict.contradicting_evidence)
            objections.append(f"evidence {cited} contradicts this explanation")

        objections.extend(verdict.concerns)
        objections.extend(verdict.unsupported_claims)

        if verdict.alternative:
            objections.append(f"a better explanation may be: {verdict.alternative}")

        return objections
