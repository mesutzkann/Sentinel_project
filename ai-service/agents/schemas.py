"""What the model is allowed to say, at each of the four points where it is asked.

Every reasoning node calls :func:`llm.structured.generate_structured` with one of these, so each
class is doing three jobs at once: it is the JSON schema constrained decoding restricts
generation to, the field descriptions the prompt shows the model, and the validation that decides
whether an answer is usable. Keeping them in one module rather than beside their nodes is
deliberate — this is the whole of the agent's contract with a language model, and it should be
readable in one sitting.

**Nothing here carries a confidence the score will use.** ``ProposedHypothesis.confidence`` is the
model's own opinion and is used for tie-breaking within ranking; ``CriticVerdict.confidence`` is
the validator term, which the formula weights at a quarter and only after the critic has said
what it checked. The number a human reads off the screen is computed in :mod:`agents.confidence`
from what was measured. A model asked "how sure are you" answers fluently and uniformly, and that
answer is not a measurement of anything.

**Free-text fields are deliberately not enums.** A title and an explanation are the parts a human
reads; constraining them would be constraining the only place the agent gets to be useful. The
one field with a vocabulary is ``category``, and even that one degrades to ``None`` rather than
failing — see :mod:`agents.categories`.

**Every field is required, and none of them started that way.** ``category`` and ``evidence``
originally carried defaults, which is the ordinary Python choice and reads as the forgiving one:
a model that omits a field gets the default instead of a validation error. It is the wrong choice
here, because these classes are not only validators — they are the grammar constrained decoding
generates against, and a field with a default is absent from the schema's ``required`` list, so
the grammar *permits* the model to skip the key. A model that follows the grammar strictly then
does exactly that.

The agent benchmark measured it: qwen2.5:7b-instruct returned ``category: null`` on all seventeen
chances while writing the right code into the ``title`` field — one root cause was titled
literally ``DB_DEADLOCK``. It looked like a model that could not categorise. It was a model that
had been told it did not have to. The same absence applied to ``evidence``, which is worse and
quieter: no citations means no evidence support, and evidence support is 45% of the confidence
score.

So every field is required and the nullable ones stay nullable. "I do not know" is still
expressible — it is ``null``, which the prompt asks for by name — but it now has to be *said*
rather than left out. The cost is that a provider without constrained decoding can fail
validation on a missing key, which spends a repair round trip; the repair message names the field,
which is the cheapest kind of retry there is.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from agents.categories import normalise_category

# Upper snake, as the backend's `Recommendation.ActionCode` and the Phase 10 approval screen
# expect. Anything else the model invents is transcribed into this shape rather than rejected:
# the code is an identifier for an action the description already states in words.
_NON_CODE = re.compile(r"[^A-Z0-9]+")

# How many candidate explanations to accept. At least one, because a run that found a single
# explanation is a real outcome — it costs the margin term of the confidence score and is
# reported as such. At most five, because the list is ranked, shown in a timeline, and re-read by
# the critic, and a sixth candidate has never been the one.
MIN_HYPOTHESES = 1
MAX_HYPOTHESES = 5

# Actions per fix plan. More than three and it stops being a recommendation and starts being a
# runbook, which is a document the knowledge base already holds.
MAX_ACTIONS = 3


class ProposedHypothesis(BaseModel):
    """One candidate explanation, as the model proposes it."""

    title: str = Field(
        description="One line naming the failure, e.g. 'Orders exhausted its connection pool'.",
    )

    description: str = Field(
        description=(
            "Two or three sentences: the mechanism, and how it produces the observed symptoms."
        ),
    )

    category: str | None = Field(
        description=(
            "The scenario code this matches, or null when none of them fits. Never guess the "
            "nearest one."
        ),
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How plausible this is compared with the other hypotheses in this list.",
    )

    evidence: list[int] = Field(
        description=(
            "Indices of the numbered evidence items that support this, and only those that do. "
            "An empty list when nothing collected supports it."
        ),
    )

    @field_validator("category", mode="after")
    @classmethod
    def _known_category_or_none(cls, value: str | None) -> str | None:
        return normalise_category(value)


class HypothesisSet(BaseModel):
    """The model's answer to "what could be causing this"."""

    hypotheses: list[ProposedHypothesis] = Field(
        min_length=MIN_HYPOTHESES,
        max_length=MAX_HYPOTHESES,
        description="Candidate explanations, most plausible first.",
    )


class RootCauseStatement(BaseModel):
    """The conclusion, written up from the hypothesis that ranked first.

    The model may correct the title and the category here, because by this point it is looking at
    one explanation and the evidence for it rather than at four at once. It may not choose *which*
    hypothesis wins — ranking did that, from what the evidence supports.
    """

    title: str = Field(description="One line a human can read as the answer.")

    category: str | None = Field(
        description="The scenario code, or null when none of them fits.",
    )

    explanation: str = Field(
        description=(
            "The causal chain, in a short paragraph: what changed, what it caused, and which "
            "evidence shows each link. Cite evidence as [n]."
        ),
    )

    evidence: list[int] = Field(
        description="Indices of the evidence items this explanation actually rests on.",
    )

    @field_validator("category", mode="after")
    @classmethod
    def _known_category_or_none(cls, value: str | None) -> str | None:
        return normalise_category(value)


class CriticVerdict(BaseModel):
    """The adversarial pass: does the evidence actually support the conclusion?

    ``valid=False`` is a first-class answer and the prompt says so. A critic that agrees with
    everything is a step in the timeline that costs a second and means nothing.
    """

    valid: bool = Field(
        description="Whether the cited evidence supports the stated cause. False is expected.",
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="How strongly the evidence supports it, independent of whether it is valid.",
    )

    concerns: list[str] = Field(
        description="What weakens the conclusion, one short line each. Empty only if nothing does.",
    )

    unsupported_claims: list[str] = Field(
        description="Statements in the explanation that no cited evidence backs.",
    )

    alternative: str | None = Field(
        description="A better explanation of the same evidence, when there is one. Else null.",
    )


class ProposedAction(BaseModel):
    """One thing to do about it."""

    action_code: str = Field(
        description="Stable identifier in upper snake case, e.g. RESTORE_POOL_SIZE.",
    )

    description: str = Field(
        description="What to do and why it addresses this cause, in one or two sentences.",
    )

    tool_name: str | None = Field(
        description="The MCP tool that would carry this out, when one exists. Otherwise null.",
    )

    @field_validator("action_code", mode="after")
    @classmethod
    def _as_code(cls, value: str) -> str:
        code = _NON_CODE.sub("_", value.strip().upper()).strip("_")

        return code or "UNSPECIFIED_ACTION"


class FixPlan(BaseModel):
    """What to do about the root cause. Advisory: every action still needs a human."""

    actions: list[ProposedAction] = Field(
        min_length=1,
        max_length=MAX_ACTIONS,
        description="Actions in the order they should be taken, safest first.",
    )
