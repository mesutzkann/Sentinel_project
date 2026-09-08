"""Getting a language model to return an object that matches a schema, reliably.

Three layers, weakest last:

1. Constrained decoding. The Pydantic model's JSON schema goes to the provider, which restricts
   generation to tokens that keep the output valid.
2. Extraction. Models wrap JSON in prose or a ``` fence even when told not to, so the parser
   tolerates both rather than failing on punctuation.
3. Repair. On a validation failure the model is shown its own output and the specific error, and
   asked again. The error text matters — "retry" alone tends to reproduce the same mistake.

Every attempt is reported, including the failures. A call that never produced valid JSON is the
most interesting row in ``model_predictions``, not one to discard.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from llm.base import LlmCompletion, LlmMessage, LlmOptions, LocalLlmProvider

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ```json ... ``` or ``` ... ```, which instruction-tuned models add unprompted.
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class StructuredOutputError(RuntimeError):
    """Every attempt failed to produce an object matching the schema."""

    def __init__(self, message: str, attempts: list[StructuredAttempt]) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True, slots=True)
class StructuredAttempt:
    """One round trip, whether or not it validated."""

    completion: LlmCompletion
    valid: bool
    error: str | None


# Written with an explicit TypeVar rather than PEP 695 syntax: the project targets Python 3.11,
# where `class StructuredResult[T]` is a syntax error.
@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    """The parsed object, and what it took to get it."""

    value: T
    attempts: list[StructuredAttempt]

    @property
    def completion(self) -> LlmCompletion:
        """The successful call."""
        return self.attempts[-1].completion

    @property
    def retries(self) -> int:
        return len(self.attempts) - 1

    @property
    def total_latency_ms(self) -> int:
        """Wall clock across every attempt, which is what the caller actually waited."""
        return sum(a.completion.latency_ms for a in self.attempts)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(a.completion.prompt_tokens for a in self.attempts)

    @property
    def total_completion_tokens(self) -> int:
        return sum(a.completion.completion_tokens for a in self.attempts)


def extract_json(text: str) -> str:
    """Pull the JSON object out of a model response.

    Strips a code fence if there is one, otherwise takes the span from the first ``{`` or ``[`` to
    its matching close, so leading apologies and trailing explanations do not break parsing.
    """
    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip()

    stripped = text.strip()

    # Whichever bracket appears first wins. Checking "{" first instead would reach inside a
    # top-level array and return its first element, silently turning a list of findings into one.
    candidates: list[tuple[int, str]] = []

    for opening, closing in (("{", "}"), ("[", "]")):
        start = stripped.find(opening)
        end = stripped.rfind(closing)
        if start != -1 and end > start:
            candidates.append((start, stripped[start : end + 1]))

    if candidates:
        return min(candidates)[1]

    return stripped


async def generate_structured(
    provider: LocalLlmProvider,
    schema: type[T],
    messages: list[LlmMessage],
    options: LlmOptions | None = None,
    max_attempts: int = 3,
) -> StructuredResult[T]:
    """Call ``provider`` until it returns something that parses as ``schema``.

    Args:
        max_attempts: Total attempts, not retries. Three is the working default: constrained
            decoding makes the first attempt succeed almost always, and a model that has failed
            twice with the error in front of it is not usually one more attempt away.

    Raises:
        StructuredOutputError: no attempt validated. Carries every attempt, so the caller can
            record the failures rather than only the fact of failure.
        LlmUnavailableError: propagated unchanged — the runtime being down is not a schema
            problem and retrying cannot fix it.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    json_schema = schema.model_json_schema()
    base_options = options or LlmOptions()
    attempt_options = LlmOptions(
        temperature=base_options.temperature,
        top_p=base_options.top_p,
        max_tokens=base_options.max_tokens,
        stop=base_options.stop,
        json_schema=json_schema,
    )

    conversation = list(messages)
    attempts: list[StructuredAttempt] = []

    for attempt in range(1, max_attempts + 1):
        completion = await provider.complete(conversation, attempt_options)

        try:
            value = schema.model_validate_json(extract_json(completion.text))
        except (ValidationError, ValueError) as exc:
            error = _describe(exc)
            attempts.append(StructuredAttempt(completion, valid=False, error=error))

            logger.warning(
                "Structured output attempt %d/%d failed for %s: %s",
                attempt,
                max_attempts,
                schema.__name__,
                error,
            )

            if attempt == max_attempts:
                break

            # The model sees its own output and what was wrong with it. Without both, the repair
            # turn is just the original request again and tends to fail the same way.
            conversation = [
                *conversation,
                LlmMessage(role="assistant", content=completion.text),
                LlmMessage(
                    role="user",
                    content=(
                        "That response did not match the required schema.\n"
                        f"Error: {error}\n\n"
                        "Reply with the corrected JSON object only. No prose, no code fence."
                    ),
                ),
            ]
            continue

        attempts.append(StructuredAttempt(completion, valid=True, error=None))
        return StructuredResult(value=value, attempts=attempts)

    raise StructuredOutputError(
        f"{schema.__name__} did not validate after {max_attempts} attempts.",
        attempts,
    )


def _describe(exc: Exception) -> str:
    """A validation error short enough to put in a prompt and specific enough to act on."""
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in err['loc']) or '(root)'}: {err['msg']}"
            for err in exc.errors()[:5]
        ]
        return "; ".join(parts)

    return str(exc)[:300]


def json_schema_hint(schema: type[BaseModel]) -> str:
    """A schema rendered for a prompt.

    Constrained decoding already forces the shape, but the schema still has to appear in the
    prompt: it carries the field descriptions, which are what tell the model what each field
    should *contain* rather than merely what type it is.
    """
    return json.dumps(_prunable(schema.model_json_schema()), indent=2)


def _prunable(schema: dict[str, Any]) -> dict[str, Any]:
    """Drops the keys that add prompt tokens without telling the model anything."""
    return {k: v for k, v in schema.items() if k not in {"title", "$defs"}} or schema
