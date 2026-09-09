"""The seam a fine-tuned model drops into.

There is one implementation today — :class:`routing.rule_router.RuleBasedRouter` — and this
abstraction exists anyway, because Phase 8 adds two more and benchmarks all three against each
other: a QLoRA-tuned 1.5B, the same model untuned with the same prompt, and the rules below as
the fallback when the model returns JSON that will not parse.

That benchmark is the point of the phase, and it is only possible if the agent depends on the
interface rather than on any of them. So the rule router is written now as a peer of the models
rather than as a thing to be replaced by them: in the shipped system it stays, as what answers
when a 1.5B model produces something unparseable.
"""

from __future__ import annotations

import abc

from routing.schema import RouteDecision


class RouterError(RuntimeError):
    """The router could not produce a decision at all.

    Distinct from an unhelpful decision. ``GENERAL_QUESTION`` is a valid answer meaning "this
    does not fit a plan"; this exception means the router itself failed, which for a model-backed
    one is a dead Ollama or JSON that survived every retry.
    """


class Router(abc.ABC):
    """Question in, plan out."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """What gets recorded against a run, e.g. ``rule`` or ``fine_tuned``.

        Written to ``investigations.router_intent``'s sibling column so a benchmark can tell
        which router produced a result months later.
        """

    @abc.abstractmethod
    async def route(self, query: str, *, service_hint: str | None = None) -> RouteDecision:
        """Classify one question.

        ``service_hint`` is what the backend already knows from the incident row — the service
        the incident was filed against. It is a hint rather than an answer because the question
        may be about a different service than the one that alerted, which is exactly what
        happens in a latency cascade.
        """
