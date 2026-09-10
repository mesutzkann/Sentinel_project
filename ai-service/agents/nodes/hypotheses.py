"""GENERATE_HYPOTHESES: the first place the agent is allowed to say what it thinks.

Everything before this state gathered facts. This one turns them into candidate explanations, and
the design of it is mostly about what it is *not* allowed to do.

*It may not conclude.* It produces a list, not an answer. Ranking picks the winner from what the
evidence supports, and the critic gets a say after that. A node that both proposed and chose
would make the choice invisible.

*It may not cite what does not exist.* Evidence indices that fall outside the collected list are
dropped and the drop is noted. A 3B model asked to cite sources will occasionally cite ``[12]``
when there are nine facts, and an out-of-range citation silently kept would inflate the evidence
support of a hypothesis that had no support at all.

*It may not reason over nothing.* With no evidence collected, the state ends the run rather than
asking the model. The confidence formula would eventually catch it — zero evidence support drags
the score under the threshold — but it would catch it three model calls later, and "the model
explained an incident it had no data about, and we then disbelieved it" is a worse timeline for a
human to read than "nothing was collected, so nothing was concluded".

**Re-entry is normal.** The state runs again after COLLECT_ADDITIONAL_EVIDENCE fetched a missing
signal, and after the critic rejected a conclusion. Both times the previous hypotheses are
replaced rather than added to: they were formed without what has since been learned, and keeping
them would let a hypothesis the critic already rejected win the next ranking.
"""

from __future__ import annotations

import logging

from agents.categories import CATEGORY_LIST
from agents.context import Hypothesis, InvestigationContext
from agents.nodes.reasoning import ReasoningNode, render_evidence, render_incident
from agents.schemas import HypothesisSet, ProposedHypothesis
from agents.state_machine import Transition
from agents.states import State
from llm.structured import StructuredOutputError

logger = logging.getLogger(__name__)


class GenerateHypothesesNode(ReasoningNode):
    """Asks the model for candidate explanations of the evidence."""

    prompt_name = "hypotheses"

    @property
    def state(self) -> State:
        return State.GENERATE_HYPOTHESES

    async def run(self, ctx: InvestigationContext) -> Transition:
        if not ctx.evidence:
            reason = (
                "no evidence was collected, so there is nothing to form a hypothesis from; "
                "the collectors either found nothing or could not be reached"
            )
            ctx.note(reason)

            return Transition(next_state=State.NEEDS_HUMAN, message=reason)

        round_number = ctx.bump("hypotheses_generated")

        try:
            result = await self.ask(
                ctx,
                HypothesisSet,
                incident=render_incident(ctx),
                evidence=render_evidence(ctx),
                categories=CATEGORY_LIST,
                feedback=self._feedback(ctx),
            )
        except StructuredOutputError as exc:
            return self.stalled(ctx, exc, producing="set of hypotheses")

        dropped = 0
        hypotheses: list[Hypothesis] = []

        for proposed in result.value.hypotheses:
            cited, out_of_range = self._citations(ctx, proposed)
            dropped += out_of_range
            hypotheses.append(
                Hypothesis(
                    title=proposed.title,
                    description=proposed.description,
                    category=proposed.category,
                    stated_confidence=proposed.confidence,
                    supporting_evidence=cited,
                )
            )

        if dropped:
            note = f"{dropped} evidence citation(s) pointed outside the collected evidence"
            ctx.note(note)
            logger.warning("GENERATE_HYPOTHESES: %s", note)

        ctx.hypotheses = hypotheses

        message = (
            f"{len(hypotheses)} hypothesis(es) from {len(ctx.evidence)} fact(s)"
            f"{'' if round_number == 1 else f', round {round_number}'}: "
            f"{'; '.join(h.title for h in hypotheses)}"
        )

        return Transition(
            next_state=State.COLLECT_ADDITIONAL_EVIDENCE,
            message=message,
            payload={
                **self.usage(result),
                "round": round_number,
                "hypotheses": [
                    {
                        "title": h.title,
                        "category": h.category,
                        "stated_confidence": h.stated_confidence,
                        "evidence": h.supporting_evidence,
                    }
                    for h in hypotheses
                ],
                "citations_dropped": dropped,
            },
        )

    @staticmethod
    def _citations(
        ctx: InvestigationContext,
        proposed: ProposedHypothesis,
    ) -> tuple[list[int], int]:
        """The cited indices that exist, deduplicated, plus a count of the ones that did not.

        Order is preserved rather than sorted: the model cites its strongest fact first often
        enough that the order carries something, and nothing downstream depends on it being
        sorted.
        """
        seen: list[int] = []
        out_of_range = 0

        for index in proposed.evidence:
            if not 0 <= index < len(ctx.evidence):
                out_of_range += 1
                continue

            if index not in seen:
                seen.append(index)

        return seen, out_of_range

    @staticmethod
    def _feedback(ctx: InvestigationContext) -> str:
        """What the critic objected to last time, or a line saying there was no last time.

        Rendered as an explicit "nothing yet" rather than an empty string, because a prompt with
        a blank section in it reads to a small model as a section it should fill in.
        """
        if not ctx.critic_feedback:
            return "(this is the first round; nothing has been rejected yet)"

        return "\n".join(f"- {line}" for line in ctx.critic_feedback)
