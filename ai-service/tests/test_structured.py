"""Structured output: extraction, repair, and what happens when repair does not work.

Driven by a scripted provider rather than a real model. The point is that the retry loop behaves
the same way every run, which a model — even at temperature 0 — cannot promise across versions.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from llm.base import LlmCompletion, LlmMessage, LlmOptions, LocalLlmProvider
from llm.structured import (
    StructuredOutputError,
    extract_json,
    generate_structured,
    json_schema_hint,
)


class Diagnosis(BaseModel):
    service: str = Field(description="The failing service.")
    confidence: float = Field(ge=0.0, le=1.0)


class ScriptedProvider(LocalLlmProvider):
    """Returns queued responses in order and records what it was asked."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.conversations: list[list[LlmMessage]] = []
        self.options: list[LlmOptions] = []

    @property
    def model(self) -> str:
        return "scripted"

    async def complete(
        self,
        messages: list[LlmMessage],
        options: LlmOptions | None = None,
    ) -> LlmCompletion:
        self.conversations.append(list(messages))
        self.options.append(options or LlmOptions())

        if not self._responses:
            raise AssertionError("the provider was called more times than the test scripted")

        return LlmCompletion(
            text=self._responses.pop(0),
            model="scripted",
            prompt_tokens=11,
            completion_tokens=7,
            latency_ms=5,
        )

    async def is_available(self) -> bool:
        return True


# ------------------------------------------------------------------- extraction ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('```json\n{"a": 1}\n```', '{"a": 1}'),
        ("```\n{\"a\": 1}\n```", '{"a": 1}'),
        ('Sure! Here is the result:\n{"a": 1}\nHope that helps.', '{"a": 1}'),
        ('[{"a": 1}]', '[{"a": 1}]'),
    ],
)
def test_extract_json_tolerates_how_models_actually_answer(raw: str, expected: str) -> None:
    assert extract_json(raw) == expected


def test_schema_hint_keeps_field_descriptions() -> None:
    """The hint exists to carry descriptions; without them it is just type noise."""
    hint = json_schema_hint(Diagnosis)

    assert "The failing service." in hint
    assert "confidence" in hint


# ------------------------------------------------------------------------ retry ----


async def test_valid_first_response_does_not_retry() -> None:
    provider = ScriptedProvider(['{"service": "payments", "confidence": 0.9}'])

    result = await generate_structured(
        provider, Diagnosis, [LlmMessage(role="user", content="what broke?")]
    )

    assert result.value.service == "payments"
    assert result.retries == 0
    assert len(provider.conversations) == 1


async def test_the_schema_is_sent_for_constrained_decoding() -> None:
    """The provider must receive the schema, or nothing is constraining generation."""
    provider = ScriptedProvider(['{"service": "orders", "confidence": 0.5}'])

    await generate_structured(
        provider, Diagnosis, [LlmMessage(role="user", content="what broke?")]
    )

    sent = provider.options[0].json_schema
    assert sent is not None
    assert "service" in sent["properties"]


async def test_invalid_response_is_retried_with_the_error_in_the_prompt() -> None:
    """A bare "try again" reproduces the same mistake; the error is what changes the answer."""
    provider = ScriptedProvider(
        [
            '{"service": "payments", "confidence": 4.2}',  # out of range
            '{"service": "payments", "confidence": 0.8}',
        ]
    )

    result = await generate_structured(
        provider, Diagnosis, [LlmMessage(role="user", content="what broke?")]
    )

    assert result.value.confidence == 0.8
    assert result.retries == 1

    repair = provider.conversations[1]
    assert repair[-2].role == "assistant", "the model must see its own failed output"
    assert "confidence" in repair[-1].content, "the repair turn must name the failing field"


async def test_usage_is_summed_across_attempts() -> None:
    """Callers waited for every attempt, so the recorded cost has to include the failed ones."""
    provider = ScriptedProvider(
        [
            "not json at all",
            '{"service": "orders", "confidence": 0.4}',
        ]
    )

    result = await generate_structured(
        provider, Diagnosis, [LlmMessage(role="user", content="what broke?")]
    )

    assert len(result.attempts) == 2
    assert result.total_prompt_tokens == 22
    assert result.total_completion_tokens == 14
    assert result.total_latency_ms == 10


async def test_giving_up_reports_every_attempt() -> None:
    """The failures are the data point; an exception that discards them loses the evidence."""
    provider = ScriptedProvider(["nope", "still nope", "nope again"])

    with pytest.raises(StructuredOutputError) as caught:
        await generate_structured(
            provider,
            Diagnosis,
            [LlmMessage(role="user", content="what broke?")],
            max_attempts=3,
        )

    assert len(caught.value.attempts) == 3
    assert all(not a.valid for a in caught.value.attempts)
    assert all(a.error for a in caught.value.attempts)


async def test_max_attempts_is_honoured() -> None:
    provider = ScriptedProvider(["nope", "still nope"])

    with pytest.raises(StructuredOutputError):
        await generate_structured(
            provider,
            Diagnosis,
            [LlmMessage(role="user", content="what broke?")],
            max_attempts=2,
        )

    assert len(provider.conversations) == 2
