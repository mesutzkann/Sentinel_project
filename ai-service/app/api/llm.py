"""The ``/llm`` endpoints: structured generation, and whether the model is there at all."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, create_model

from app.config import Settings, settings
from llm.base import LlmMessage, LlmOptions, LlmUnavailableError, LocalLlmProvider
from llm.ollama_provider import OllamaLlmProvider
from llm.prompts import PromptNotFoundError, registry
from llm.structured import StructuredOutputError, generate_structured, json_schema_hint
from reporting.predictions import ModelPurpose, PredictionRecord, PredictionReporter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/llm", tags=["llm"])


# ------------------------------------------------------------------ dependencies ----


def get_settings() -> Settings:
    return settings()


def get_provider(config: Annotated[Settings, Depends(get_settings)]) -> LocalLlmProvider:
    return OllamaLlmProvider(
        base_url=config.ollama_base_url,
        model=config.llm_model,
        timeout_seconds=config.llm_timeout_seconds,
    )


def get_reporter(config: Annotated[Settings, Depends(get_settings)]) -> PredictionReporter:
    return PredictionReporter(
        base_url=config.backend_base_url,
        internal_token=config.internal_api_key,
    )


# ---------------------------------------------------------------------- contracts ----


class StructuredRequest(BaseModel):
    """A prompt plus the shape the answer has to take."""

    prompt: str = Field(min_length=1, description="What to ask. Rendered into the user turn.")

    json_schema: dict[str, Any] = Field(
        description=(
            "JSON Schema the response must match. Object schemas with a `properties` map; "
            "`required` is honoured."
        )
    )

    system: str | None = Field(
        default=None,
        description="Optional system turn. Overrides the prompt template's own instructions.",
    )

    prompt_name: str | None = Field(
        default=None,
        description=(
            "A registered prompt to wrap the input in, e.g. `structured_extraction`. "
            "Without it the prompt is sent as written."
        ),
    )

    prompt_version: str = "v1"

    purpose: ModelPurpose = Field(
        default=ModelPurpose.REASONING,
        description="Recorded against the call, so the dashboard can report cost per purpose.",
    )

    investigation_id: str | None = None

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    max_attempts: int | None = Field(
        default=None, ge=1, le=5, description="Overrides the configured default."
    )


class StructuredUsage(BaseModel):
    """What the call cost, summed across attempts."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    attempts: int
    retries: int


class StructuredResponse(BaseModel):
    data: dict[str, Any] = Field(description="The model's answer, validated against the schema.")
    usage: StructuredUsage
    recorded: bool = Field(
        description="Whether the call was persisted as a model_predictions row."
    )


class LlmHealth(BaseModel):
    available: bool
    model: str
    base_url: str
    detail: str


# ---------------------------------------------------------------------- endpoints ----


@router.get("/health", response_model=LlmHealth)
async def llm_health(
    provider: Annotated[LocalLlmProvider, Depends(get_provider)],
    config: Annotated[Settings, Depends(get_settings)],
) -> LlmHealth:
    """Whether the runtime is up and has the configured model.

    Returns 200 with ``available: false`` rather than an error status: this is a report about a
    dependency, and a health check that fails to answer is less useful than one that answers
    "no".
    """
    available = await provider.is_available()

    return LlmHealth(
        available=available,
        model=config.llm_model,
        base_url=config.ollama_base_url,
        detail=(
            "Model is loaded and reachable."
            if available
            else f"Run: ollama pull {config.llm_model}"
        ),
    )


@router.post("/structured", response_model=StructuredResponse)
async def structured(
    request: StructuredRequest,
    provider: Annotated[LocalLlmProvider, Depends(get_provider)],
    reporter: Annotated[PredictionReporter, Depends(get_reporter)],
    config: Annotated[Settings, Depends(get_settings)],
) -> StructuredResponse:
    """Generate JSON that conforms to a caller-supplied schema.

    The schema is enforced during decoding where the runtime supports it, and validated
    afterwards regardless. Failed attempts are recorded too — a call that never produced valid
    JSON is the data point the structured-output success rate is built from.
    """
    try:
        response_model = _model_from_schema(request.json_schema)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"json_schema is not usable: {exc}",
        ) from exc

    messages = _build_messages(request, response_model)
    options = LlmOptions(temperature=request.temperature)
    max_attempts = request.max_attempts or config.llm_max_attempts

    try:
        result = await generate_structured(
            provider=provider,
            schema=response_model,
            messages=messages,
            options=options,
            max_attempts=max_attempts,
        )
    except LlmUnavailableError as exc:
        # The runtime, not the request. 503 so a caller can tell "start Ollama" apart from
        # "your schema is wrong", and retrying the same prompt is pointless either way.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except StructuredOutputError as exc:
        await _record_failure(reporter, request, exc, provider.model)

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": str(exc),
                "attempts": [
                    {"error": a.error, "output": a.completion.text[:500]} for a in exc.attempts
                ],
            },
        ) from exc

    recorded = await reporter.record(
        PredictionRecord(
            model_name=result.completion.model,
            purpose=request.purpose,
            prompt_tokens=result.total_prompt_tokens,
            completion_tokens=result.total_completion_tokens,
            latency_ms=result.total_latency_ms,
            valid_json=True,
            output=result.value.model_dump_json(),
            investigation_id=request.investigation_id,
        )
    )

    return StructuredResponse(
        data=result.value.model_dump(),
        usage=StructuredUsage(
            model=result.completion.model,
            prompt_tokens=result.total_prompt_tokens,
            completion_tokens=result.total_completion_tokens,
            latency_ms=result.total_latency_ms,
            attempts=len(result.attempts),
            retries=result.retries,
        ),
        recorded=recorded,
    )


# ------------------------------------------------------------------------ helpers ----


def _build_messages(
    request: StructuredRequest,
    response_model: type[BaseModel],
) -> list[LlmMessage]:
    """Assembles the turns, optionally through a registered prompt."""
    messages: list[LlmMessage] = []

    if request.system:
        messages.append(LlmMessage(role="system", content=request.system))

    if request.prompt_name is None:
        messages.append(LlmMessage(role="user", content=request.prompt))
        return messages

    try:
        prompt = registry().get(request.prompt_name, request.prompt_version)
    except PromptNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    # Templates take `schema` and one input variable; whichever the template declares is filled
    # from `prompt`, so a template can call it `input` or `observations` without this knowing.
    values: dict[str, object] = {"schema": json_schema_hint(response_model)}
    for variable in prompt.variables:
        if variable != "schema":
            values[variable] = request.prompt

    messages.append(LlmMessage(role="user", content=prompt.render(**values)))
    return messages


def _model_from_schema(schema: dict[str, Any]) -> type[BaseModel]:
    """Builds a Pydantic model from a caller-supplied JSON Schema.

    Only the subset the endpoint actually needs: a flat object of typed, optionally required
    fields. Nested objects and arrays of objects are accepted as ``dict``/``list``, which keeps
    validation honest about what it checked rather than silently ignoring deeper constraints.
    """
    if schema.get("type") != "object":
        raise ValueError("the root schema must have \"type\": \"object\"")

    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("the root schema must have a non-empty \"properties\" map")

    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}

    for name, spec in properties.items():
        if not isinstance(spec, dict):
            raise ValueError(f"property '{name}' must be an object")

        annotation = _annotation(spec)
        description = spec.get("description")

        if name in required:
            fields[name] = (annotation, Field(description=description))
        else:
            fields[name] = (annotation | None, Field(default=None, description=description))

    return create_model("StructuredOutput", **fields)


_SCALARS: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _annotation(spec: dict[str, Any]) -> Any:
    """Maps one JSON Schema property to a Python annotation."""
    if "enum" in spec:
        values = spec["enum"]

        if not isinstance(values, list) or not values:
            raise ValueError("\"enum\" must be a non-empty list")

        # Literal rather than str. The annotation is what produces the schema sent for
        # constrained decoding and what Pydantic validates against, so widening an enum to str
        # here drops the constraint in both places at once — the model is free to invent a
        # category, and validation accepts it. That is the failure this endpoint exists to
        # prevent, and it is silent.
        return Literal[tuple(values)]  # type: ignore[return-value]

    declared = spec.get("type", "string")

    if isinstance(declared, list):
        # ["string", "null"] is how optionality is usually written. Nullability is handled by
        # `required`, so the null is dropped and the remaining type used.
        non_null = [t for t in declared if t != "null"]
        declared = non_null[0] if non_null else "string"

    if declared == "array":
        items = spec.get("items")
        if isinstance(items, dict):
            return list[_annotation(items)]  # type: ignore[misc]
        return list

    return _SCALARS.get(declared, str)


async def _record_failure(
    reporter: PredictionReporter,
    request: StructuredRequest,
    error: StructuredOutputError,
    model: str,
) -> None:
    """Records a call that never validated, so failures are visible in the same table."""
    if not error.attempts:
        return

    await reporter.record(
        PredictionRecord(
            model_name=model,
            purpose=request.purpose,
            prompt_tokens=sum(a.completion.prompt_tokens for a in error.attempts),
            completion_tokens=sum(a.completion.completion_tokens for a in error.attempts),
            latency_ms=sum(a.completion.latency_ms for a in error.attempts),
            valid_json=False,
            output=error.attempts[-1].completion.text[:4000],
            investigation_id=request.investigation_id,
        )
    )
