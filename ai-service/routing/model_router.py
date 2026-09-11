"""Routing with a language model: the fine-tuned one, and the base it has to beat.

One class rather than the `FineTunedRouter` and `BaseModelRouter` the planning document names,
because the only difference between them is which model answers. A second class would be a copy
of this one, and the copy would drift — and then the benchmark would be comparing two code paths
as much as two models, which is the one thing it must not do.

**Both are given the same prompt.** The tuned model will be trained on that prompt as its input,
so it pays the same prefill; giving the base model a longer one to make up for what it was never
taught would measure prompt engineering rather than fine-tuning.

``constrained`` is the other knob worth understanding. With it on — which is what ships — the
schema is a grammar the decoder cannot leave, so the answer is always shaped correctly and
"invalid JSON rate" is zero by construction. With it off the model is on its own, and that number
becomes a measurement of the model rather than of llama.cpp. The benchmark runs both, because
both are true and only one of them is what the agent experiences.
"""

from __future__ import annotations

import logging

from llm.base import LlmMessage, LlmOptions, LocalLlmProvider
from llm.prompts import PromptRegistry, registry
from llm.structured import (
    StructuredOutputError,
    extract_json,
    generate_structured,
    json_schema_hint,
)
from routing.base import Router, RouterError
from routing.plans import INTENT_PLANS, decide
from routing.rule_router import KNOWN_SERVICES
from routing.schema import Intent, RouteDecision

logger = logging.getLogger(__name__)

# Generous for a route object, which is five fields and about a hundred tokens. This only applies
# without the grammar; with it, decoding stops when the object closes.
UNCONSTRAINED_MAX_TOKENS = 300


def intent_table() -> str:
    """The fifteen intents and the tools each implies, rendered from the table itself.

    Generated rather than written into the prompt file, so a plan that changes in
    `routing/plans.py` cannot leave the prompt describing the old one.
    """
    lines = []

    for intent in Intent:
        requires_rag, requires_mcp, tools = INTENT_PLANS[intent]
        flags = f"rag={str(requires_rag).lower()} mcp={str(requires_mcp).lower()}"
        listed = ", ".join(tools) if tools else "(none)"
        lines.append(f"- {intent.value}: {flags}, tools: {listed}")

    return "\n".join(lines)


class ModelRouter(Router):
    """A model that answers with a :class:`RouteDecision`."""

    def __init__(
        self,
        provider: LocalLlmProvider,
        *,
        name: str = "model",
        prompt_version: str = "v1",
        constrained: bool = True,
        fallback: Router | None = None,
        prompts: PromptRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._name = name
        self._prompt_version = prompt_version
        self._constrained = constrained
        self._fallback = fallback
        self._prompts = prompts or registry()

        # Counted rather than logged, because the share of answers that would not parse is one of
        # the numbers this phase exists to report.
        self.invalid_json = 0
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def render(self, query: str) -> str:
        prompt = self._prompts.get("router", self._prompt_version)

        return prompt.render(
            schema=json_schema_hint(RouteDecision),
            intents=intent_table(),
            services=", ".join(KNOWN_SERVICES),
            query=query,
        )

    async def route(self, query: str, *, service_hint: str | None = None) -> RouteDecision:
        """Classify one question.

        ``service_hint`` is *not* put in the prompt. It is what the backend already knows from
        the incident row, and a router told the answer would score well here and teach us
        nothing; it is applied afterwards, only when the model named no service itself.
        """
        self.calls += 1
        rendered = self.render(query)

        try:
            decision = await self._ask(rendered)
        except StructuredOutputError as exc:
            self.invalid_json += 1
            logger.info(
                "%s produced no usable route after %d attempt(s)", self._name, len(exc.attempts)
            )

            return await self._fall_back(query, service_hint)
        except ValueError:
            # Unconstrained decoding that came back as something other than the object asked for.
            self.invalid_json += 1

            return await self._fall_back(query, service_hint)

        if decision.target_service is None and service_hint is not None:
            decision = decision.model_copy(update={"target_service": service_hint})

        # The plan is not the model's to invent. Whatever it emitted for the flags and the tools,
        # what leaves here is what `routing/plans.py` says the intent implies — the benchmark
        # scores the raw answer, and the agent gets the consistent one.
        return decide(decision.intent, decision.target_service)

    async def raw(self, query: str) -> RouteDecision:
        """The model's answer as it came, for the benchmark. Raises on anything unusable."""
        return await self._ask(self.render(query))

    async def _ask(self, rendered: str) -> RouteDecision:
        messages = [LlmMessage(role="user", content=rendered)]

        if self._constrained:
            result = await generate_structured(
                provider=self._provider,
                schema=RouteDecision,
                messages=messages,
                options=LlmOptions(),
                max_attempts=1,
            )

            return result.value

        # Unconstrained: one attempt, parsed by hand, and a failure is a failure. No repair loop,
        # because the number being measured is whether the model can produce the shape unaided.
        #
        # Capped, though. A route is about a hundred tokens; left uncapped the 1.5B rambled to
        # the end of its context on every question and the benchmark measured fourteen seconds
        # of that rather than whether the answer was usable.
        completion = await self._provider.complete(
            messages, LlmOptions(max_tokens=UNCONSTRAINED_MAX_TOKENS)
        )

        return RouteDecision.model_validate_json(extract_json(completion.text))

    async def _fall_back(self, query: str, service_hint: str | None) -> RouteDecision:
        """Answer badly rather than fail the run.

        This is the third job `routing/rule_router.py` was written for. A 1.5B that returns
        something unparseable leaves the investigation with no plan at all, and a keyword table's
        guess is worth more than that.
        """
        if self._fallback is None:
            raise RouterError(f"{self._name} produced no usable route and has no fallback")

        return await self._fallback.route(query, service_hint=service_hint)
