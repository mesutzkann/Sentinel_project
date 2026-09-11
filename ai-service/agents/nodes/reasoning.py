"""What the four thinking nodes share: one model call, one prompt, and what the model may see.

The collectors summarise deterministically (see :mod:`agents.nodes.collect`) precisely so that
this layer has short lines to work with. This module is where those lines become a prompt, and
the constraint it exists to enforce is size: twenty-five tool outputs do not fit in an 8k window,
and a reasoning node that pasted raw dictionaries into a prompt would either overflow the context
or push the question itself out of the model's attention.

So evidence reaches a prompt as a numbered list of one-line facts. The numbers are load-bearing:
they are indices into ``ctx.evidence``, they are what a hypothesis cites, and they are what the
confidence score counts. That is why a truncated list still prints its real indices and says how
many it left out — renumbering to make a tidy list would silently re-point every citation.

**The raw output is not in the prompt and is not lost.** ``EvidenceItem.raw`` holds what the tool
returned, the callback carries it to the backend, and a human checking a conclusion reads it
there. What the model needs to reason is what the fact *is*; what a human needs to audit is the
bytes it came from, and those are different audiences.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

from pydantic import BaseModel

from agents.context import InvestigationContext
from agents.state_machine import Node, Transition
from agents.states import State
from llm.base import LlmMessage, LlmOptions, LocalLlmProvider
from llm.prompts import PromptRegistry, registry
from llm.structured import (
    StructuredOutputError,
    StructuredResult,
    generate_structured,
    json_schema_hint,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# One evidence line, capped. The collectors already write short summaries; the cap is against a
# server whose `interpretation` field grows, and it is generous enough that no current tool
# reaches it.
EVIDENCE_LINE_CHARS = 240

# How many facts reach a prompt. Twenty-five tool calls can produce more than this, and the ones
# that fall off are the lowest weighted — a collector that looked and found nothing carries 0.4,
# a measurement that found something carries 0.8, and if something has to go it is the negative.
MAX_EVIDENCE_LINES = 24

# Attempts per structured call, not retries. Matches `Settings.llm_max_attempts`; passed in by
# whoever builds the nodes so a benchmark can turn repair off and measure the model rather than
# the loop.
DEFAULT_MAX_ATTEMPTS = 3


def render_evidence(ctx: InvestigationContext) -> str:
    """The evidence as a numbered list, at most :data:`MAX_EVIDENCE_LINES` lines long.

    Indices are the positions in ``ctx.evidence`` and never change, so a hypothesis that cites
    ``[3]`` cites the same fact in every prompt of the run and in the database afterwards.
    """
    if not ctx.evidence:
        return "(no evidence was collected)"

    numbered = list(enumerate(ctx.evidence))

    if len(numbered) > MAX_EVIDENCE_LINES:
        # Sort by weight to choose, then back into index order to print: a list that jumped from
        # [7] to [2] reads as a mistake, and the model has to be able to scan it.
        kept = sorted(numbered, key=lambda pair: pair[1].weight, reverse=True)
        kept = sorted(kept[:MAX_EVIDENCE_LINES], key=lambda pair: pair[0])
        omitted = len(numbered) - len(kept)
    else:
        kept = numbered
        omitted = 0

    lines = [
        f"[{index}] ({item.source}, weight {item.weight:.2f}) "
        f"{_one_line(item.summary)[:EVIDENCE_LINE_CHARS]}"
        for index, item in kept
    ]

    if omitted:
        lines.append(f"({omitted} further item(s) of lower weight omitted from this list)")

    return "\n".join(lines)


def render_hypotheses(ctx: InvestigationContext) -> str:
    """The hypotheses as the critic and the ranker's readers see them."""
    if not ctx.hypotheses:
        return "(none)"

    return "\n".join(
        f"{position}. {h.title} [{h.category or 'uncategorised'}] "
        f"— evidence {h.supporting_evidence or 'none cited'}: {_one_line(h.description)}"
        for position, h in enumerate(ctx.hypotheses, start=1)
    )


def render_incident(ctx: InvestigationContext) -> str:
    """The question, the incident and the service, which every reasoning prompt opens with."""
    return (
        f"Incident {ctx.incident_code} on {ctx.target_service or 'an unnamed service'}.\n"
        f"Question: {ctx.query}"
    )


class ReasoningNode(Node):
    """A state whose work is one structured call to a language model."""

    #: The prompt in ``llm/prompts/``. Set by the subclass; the version is chosen per instance so
    #: Phase 11 can run two prompt versions against the same scenarios.
    prompt_name: str = ""

    #: Which version a caller gets when it does not pin one. Per node rather than one number for
    #: the build, because prompts are improved one at a time: the critic is on v2 because v1 was
    #: measured and rejected correct conclusions, and that says nothing about the other four.
    default_prompt_version: str = "v1"

    def __init__(
        self,
        provider: LocalLlmProvider,
        *,
        prompt_version: str | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._prompt_version = prompt_version or self.default_prompt_version
        self._max_attempts = max_attempts
        self._prompts = prompts or registry()

    @property
    def prompt_id(self) -> str:
        """``name.version``, recorded on every step this node produces.

        A run whose conclusion cannot be traced to the exact prompt that produced it cannot be
        compared against another run, which is the whole of what Phase 11 does.
        """
        return f"{self.prompt_name}.{self._prompt_version}"

    async def ask(
        self,
        ctx: InvestigationContext,
        schema: type[T],
        **values: object,
    ) -> StructuredResult[T]:
        """Render this node's prompt, call the model, and book what it cost.

        Raises:
            StructuredOutputError: no attempt validated. The cost of the failed attempts is
                recorded on the context first — a run that spent three round trips and got
                nothing spent them, and a report that only counts successful calls would show
                the failure as free.
            LlmUnavailableError: propagated. The runtime being down is an environment defect
                rather than an outcome of the investigation, and the runner ends the run in
                FAILED where a human looking at the timeline will read it as one.
        """
        prompt = self._prompts.get(self.prompt_name, self._prompt_version)
        rendered = prompt.render(schema=json_schema_hint(schema), **values)

        try:
            result = await generate_structured(
                provider=self._provider,
                schema=schema,
                messages=[LlmMessage(role="user", content=rendered)],
                options=LlmOptions(),
                max_attempts=self._max_attempts,
            )
        except StructuredOutputError as exc:
            ctx.record_llm(
                prompt_tokens=sum(a.completion.prompt_tokens for a in exc.attempts),
                completion_tokens=sum(a.completion.completion_tokens for a in exc.attempts),
                calls=len(exc.attempts),
            )
            raise

        ctx.record_llm(
            prompt_tokens=result.total_prompt_tokens,
            completion_tokens=result.total_completion_tokens,
            calls=len(result.attempts),
        )

        return result

    def stalled(
        self,
        ctx: InvestigationContext,
        exc: StructuredOutputError,
        *,
        producing: str,
    ) -> Transition:
        """End the run in NEEDS_HUMAN because the model would not produce a usable answer.

        Not FAILED. The model answered — it just never answered in the shape it was asked for,
        which is a fact about a 3B model rather than a defect in the agent, and the evidence the
        run collected is still worth a person's time. The number of attempts is in the message
        because "the model could not produce hypotheses" and "it could not do so three times" are
        different things to read at three in the morning.
        """
        reason = (
            f"the model did not produce a usable {producing} after "
            f"{len(exc.attempts)} attempt(s): {exc}"
        )
        ctx.note(reason)
        logger.warning("%s: %s", self.state, reason)

        return Transition(
            next_state=State.NEEDS_HUMAN,
            message=reason,
            payload={"prompt_id": self.prompt_id, "attempts": len(exc.attempts)},
        )

    def usage(self, result: StructuredResult[Any]) -> dict[str, Any]:
        """What one call cost, for the step payload."""
        return {
            "prompt_id": self.prompt_id,
            "model": result.completion.model,
            "llm_latency_ms": result.total_latency_ms,
            "llm_retries": result.retries,
            "prompt_tokens": result.total_prompt_tokens,
            "completion_tokens": result.total_completion_tokens,
        }


def _one_line(text: str) -> str:
    """Collapse whitespace. A newline inside an evidence line breaks the numbered list."""
    return " ".join(text.split())
