"""SELECT_ROOT_CAUSE: writing up the hypothesis that won, without letting it re-run the contest.

Ranking already chose. What is missing after ranking is a causal account a person can read — the
hypotheses are one line and a sketch, and "Orders exhausted its connection pool (0.71)" is not an
answer anyone can act on. So this state asks the model for one thing: the chain, with each link
attached to a piece of evidence.

**It sees the runner-up and cannot pick it.** The alternatives go into the prompt because the
useful part of a root cause writeup is often why the obvious other explanation does not fit, and
a model asked to explain one hypothesis in isolation writes a defence of it instead. The schema
has no field for choosing, so the alternative can be argued against and cannot be substituted.

**Two fallbacks, both of which prefer the ranked hypothesis to the model's silence.** If the
statement comes back with no category, the hypothesis's category stands; if it cites no evidence,
the hypothesis's citations stand. The hypothesis was formed while looking at the same evidence
and it is what actually won, so its answer is the better default — and an empty citation list
would drive evidence support to zero and sink a conclusion that was in fact supported. Both
fallbacks are noted when they happen, because "the model declined to categorise" is worth seeing.

The step emits no root cause event. That event carries a confidence, and confidence is not known
until the critic has spoken — so VALIDATE emits it, once, complete.
"""

from __future__ import annotations

import logging

from agents.categories import CATEGORY_LIST
from agents.context import InvestigationContext, RootCause
from agents.nodes.reasoning import ReasoningNode, render_evidence, render_incident
from agents.schemas import RootCauseStatement
from agents.state_machine import Transition
from agents.states import State
from llm.structured import StructuredOutputError

logger = logging.getLogger(__name__)


class SelectRootCauseNode(ReasoningNode):
    """Turns the top-ranked hypothesis into a stated cause with a causal explanation."""

    prompt_name = "root_cause"

    @property
    def state(self) -> State:
        return State.SELECT_ROOT_CAUSE

    async def run(self, ctx: InvestigationContext) -> Transition:
        if not ctx.hypotheses:
            reason = "there is no ranked hypothesis to write up"
            ctx.note(reason)

            return Transition(next_state=State.NEEDS_HUMAN, message=reason)

        winner = ctx.hypotheses[0]

        for hypothesis in ctx.hypotheses:
            hypothesis.selected = hypothesis is winner

        try:
            result = await self.ask(
                ctx,
                RootCauseStatement,
                incident=render_incident(ctx),
                evidence=render_evidence(ctx),
                categories=CATEGORY_LIST,
                hypothesis=(
                    f"{winner.title} [{winner.category or 'uncategorised'}] "
                    f"(score {winner.score:.2f}, evidence {winner.supporting_evidence})\n"
                    f"{winner.description}"
                ),
                alternatives=self._alternatives(ctx),
            )
        except StructuredOutputError as exc:
            return self.stalled(ctx, exc, producing="root cause statement")

        statement = result.value
        cited = [index for index in statement.evidence if 0 <= index < len(ctx.evidence)]

        if not cited:
            ctx.note(
                "the root cause statement cited no usable evidence; "
                f"the citations of '{winner.title}' stand instead"
            )
            cited = list(winner.supporting_evidence)

        category = statement.category or winner.category

        if statement.category is None and winner.category is not None:
            ctx.note(f"the statement named no category; the hypothesis said {winner.category}")
        elif (
            statement.category is not None
            and winner.category is not None
            and statement.category != winner.category
        ):
            ctx.note(
                f"the statement recategorised the conclusion from {winner.category} "
                f"to {statement.category}"
            )

        ctx.root_cause = RootCause(
            title=statement.title,
            explanation=statement.explanation,
            category=category,
            hypothesis_title=winner.title,
            supporting_evidence=cited,
        )

        return Transition(
            next_state=State.VALIDATE,
            message=f"root cause: {statement.title} [{category or 'uncategorised'}]",
            payload={
                **self.usage(result),
                "title": statement.title,
                "category": category,
                "evidence": cited,
                "from_hypothesis": winner.title,
                "hypothesis_score": winner.score,
            },
        )

    @staticmethod
    def _alternatives(ctx: InvestigationContext) -> str:
        """The hypotheses that lost, for the explanation to rule out.

        A run with a single hypothesis says so explicitly. The alternative — an empty section —
        invites a small model to fill it with an alternative it has just invented.
        """
        losers = ctx.hypotheses[1:]

        if not losers:
            return "(none; this was the only hypothesis, which is itself worth stating)"

        return "\n".join(
            f"- {h.title} (score {h.score:.2f}): {h.description}" for h in losers
        )
