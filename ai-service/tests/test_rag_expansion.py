"""Query expansion: the probe, and everything that can go wrong producing one.

The generated passage is never shown to anyone and never used as evidence, so its truth is not
the property under test. What matters is that a bad generation costs a search nothing: a model
that is down, that answers in one word, or that wraps the passage in "Sure, here is…" must all
end with retrieval doing what it did before expansion existed.
"""

from __future__ import annotations

import pytest

from llm.base import LlmCompletion, LlmMessage, LlmOptions, LlmUnavailableError, LocalLlmProvider
from rag.expansion import HydeExpander


class _FakeProvider(LocalLlmProvider):
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[list[LlmMessage]] = []
        self.options: LlmOptions | None = None

    @property
    def model(self) -> str:
        return "qwen2.5:3b-instruct"

    async def complete(
        self,
        messages: list[LlmMessage],
        options: LlmOptions | None = None,
    ) -> LlmCompletion:
        if self._error is not None:
            raise self._error

        self.calls.append(messages)
        self.options = options

        return LlmCompletion(
            text=self._text,
            model=self.model,
            prompt_tokens=120,
            completion_tokens=60,
            latency_ms=800,
        )

    async def is_available(self) -> bool:
        return self._error is None


PASSAGE = (
    "Loki stores every log line from the five services, labelled by service_name. A query for "
    "one service filters on that label and pipes the result through the json parser. The label "
    "is service_name rather than job, which is what Prometheus uses for the same idea."
)


# ------------------------------------------------------------------ the probe ----


async def test_the_question_reaches_the_model() -> None:
    provider = _FakeProvider(PASSAGE)

    expansion = await HydeExpander(provider).expand("loglar nerede tutuluyor")

    assert expansion is not None
    assert "loglar nerede tutuluyor" in provider.calls[0][0].content
    assert expansion.text == PASSAGE
    assert expansion.prompt_id == "hyde_passage.v1"


async def test_generation_is_deterministic_and_bounded() -> None:
    """Two searches for the same question must probe with the same passage.

    A sampled expansion would make the same query return different documents on two runs, which
    is a retrieval evaluation measuring the sampler.
    """
    provider = _FakeProvider(PASSAGE)

    await HydeExpander(provider, max_tokens=200).expand("why is orders slow")

    assert provider.options is not None
    assert provider.options.temperature == 0.0
    assert provider.options.max_tokens == 200


async def test_the_model_is_named_on_the_expansion() -> None:
    # Which model wrote the probe is part of reproducing a benchmark row.
    expansion = await HydeExpander(_FakeProvider(PASSAGE)).expand("query")

    assert expansion is not None
    assert expansion.model == "qwen2.5:3b-instruct"


# --------------------------------------------------------------- what can fail ----


async def test_a_dead_model_runtime_costs_the_search_nothing() -> None:
    """Expansion is the optional half of the dense half.

    Without it retrieval is exactly Phase 5, which answered every one of these queries. Letting
    a generation failure propagate would trade a working search for a better one.
    """
    provider = _FakeProvider(error=LlmUnavailableError("Ollama is not running"))

    assert await HydeExpander(provider).expand("query") is None


async def test_an_unexpected_error_is_also_swallowed() -> None:
    provider = _FakeProvider(error=RuntimeError("something else entirely"))

    assert await HydeExpander(provider).expand("query") is None


async def test_a_one_word_answer_is_not_used() -> None:
    # A model that misunderstood the instruction produces a probe made of noise, and fusing a
    # ranking built on noise is worse than not having the ranking.
    assert await HydeExpander(_FakeProvider("Deadlock.")).expand("query") is None


async def test_an_empty_answer_is_not_used() -> None:
    assert await HydeExpander(_FakeProvider("   ")).expand("query") is None


# ------------------------------------------------------------------- cleaning ----


async def test_a_chat_models_preamble_is_removed() -> None:
    provider = _FakeProvider(f"Sure, here is the passage:\n{PASSAGE}")

    expansion = await HydeExpander(provider).expand("query")

    assert expansion is not None
    assert expansion.text.startswith("Loki stores")


async def test_a_code_fence_is_removed() -> None:
    provider = _FakeProvider(f"```\n{PASSAGE}\n```")

    expansion = await HydeExpander(provider).expand("query")

    assert expansion is not None
    assert "```" not in expansion.text


async def test_a_passage_that_needs_no_cleaning_is_untouched() -> None:
    expansion = await HydeExpander(_FakeProvider(PASSAGE)).expand("query")

    assert expansion is not None
    assert expansion.text == PASSAGE


# --------------------------------------------------------------------- prompt ----


def test_the_prompt_demands_english_and_no_preamble() -> None:
    """The two instructions the failing benchmark queries depend on.

    English because the corpus is English and half the point of expansion here is bridging a
    Turkish question to it; no preamble because the passage is embedded verbatim, and "Sure,
    here is a passage about" is three tokens of the probe spent on nothing.
    """
    from llm.prompts import registry

    template = registry().get("hyde_passage").template

    assert "Write in English" in template
    assert "no preamble" in template


@pytest.mark.parametrize("query", ["", "   "])
async def test_an_empty_query_still_produces_no_crash(query: str) -> None:
    assert await HydeExpander(_FakeProvider("Deadlock.")).expand(query) is None
