"""The provider boundary: what SentinelAI needs from a language model, and nothing more.

Everything above this module — structured output, the router, the agent — is written against
these types rather than against Ollama. That is what makes the Phase 8 benchmark possible: the
fine-tuned router and its base model are two providers measured through one interface.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class LlmMessage:
    """One turn. ``role`` is ``system``, ``user`` or ``assistant``."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LlmCompletion:
    """What came back, plus what it cost.

    The cost fields are not incidental. Every call is recorded as a ``model_predictions`` row,
    and the Phase 11 dashboard reports latency and tokens per purpose from them, so a provider
    that cannot report token counts has to say so with zeros rather than omit them.
    """

    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class LlmOptions:
    """Generation settings.

    ``temperature`` defaults to 0. Every use in this project — routing, extraction, root cause,
    validation — wants the same answer for the same evidence twice, and an evaluation suite over
    a sampling model measures the sampler as much as the model.
    """

    temperature: float = 0.0
    top_p: float = 1.0
    max_tokens: int | None = None
    stop: list[str] = field(default_factory=list)

    # A JSON schema the response must conform to. Providers that support constrained decoding
    # enforce it during generation; the rest fall back to asking for JSON and validating after.
    json_schema: dict[str, Any] | None = None


class LlmError(RuntimeError):
    """The provider could not produce a completion."""


class LlmUnavailableError(LlmError):
    """The model runtime could not be reached, or does not have the model.

    Separate from :class:`LlmError` because it is the one failure that is about the environment
    rather than the request: the caller should surface "Ollama is not running" rather than retry
    a prompt that was never the problem.
    """


class LocalLlmProvider(abc.ABC):
    """A local model runtime.

    Called ``ILocalLlmProvider`` in docs/planning.md; the ``I`` prefix is dropped here because
    Python does not use it and the abstractness is already carried by the ABC.
    """

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Model identifier as the runtime knows it, e.g. ``qwen2.5:3b-instruct``."""

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[LlmMessage],
        options: LlmOptions | None = None,
    ) -> LlmCompletion:
        """Generate a completion.

        Raises:
            LlmUnavailableError: the runtime is unreachable or the model is not present.
            LlmError: the runtime answered, but not with a completion.
        """

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Whether the runtime is reachable and has :attr:`model`.

        Used by the health endpoint, so it must not raise: an unreachable runtime is a ``False``,
        not an exception.
        """
