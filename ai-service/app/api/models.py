"""The ``/models`` endpoints: which models this service runs, and what the router is worth.

Phase 8 replaced a keyword table with a fine-tuned 1.5B, and the only honest way to show that is
to show the measurement — so this publishes the benchmark's own JSON report rather than numbers
typed into a page. If nobody has run `router_eval --output`, the page says so instead of showing
a stale table; a number with no run behind it is worse than no number.

``POST /models/route`` is the other half: the same question through the router the agent actually
uses **and** through the keyword table, side by side. That is the phase's claim made checkable by
anyone who can type a question, and it is also the quickest way to see the fallback working —
when the model is not imported, both columns come back identical, because the model router
answered with the table.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints

from app.config import Settings, settings
from llm.ollama_provider import OllamaLlmProvider
from routing.base import Router as RouterProtocol
from routing.base import RouterError
from routing.factory import build_router
from routing.rule_router import RuleBasedRouter
from routing.schema import RouteDecision

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

# ai-service/app/api/models.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Where `python -m evaluation.router_eval --split test --models sentinel-router --prompt v2
#: --output datasets/routing/benchmark.json` puts its report. A convention rather than a setting:
#: the page reports the benchmark this repository ships, and pointing it somewhere else would
#: make what the page claims depend on how the process was started.
BENCHMARK_REPORT = _REPO_ROOT / "datasets" / "routing" / "benchmark.json"


# ---------------------------------------------------------------------- contracts ----


class ModelSummary(BaseModel):
    """One model this service is configured to use, and whether it is actually there."""

    purpose: Literal["reasoning", "routing", "embedding"]
    name: str
    available: bool
    detail: str


class RouterSummary(BaseModel):
    """Which router answers, and what happens when it cannot."""

    active: str = Field(description="`fine_tuned` or `rule`, matching the plan event's `router`.")
    model: str | None = Field(description="None when routing by keyword table alone.")
    prompt_version: str | None = None
    timeout_seconds: float | None = None
    fallback: str | None = Field(
        default=None,
        description="What answers when the model is unparseable or absent.",
    )


class BenchmarkRow(BaseModel):
    """One router's row in the published comparison."""

    router: str
    examples: int
    intent_accuracy: float
    service_accuracy: float
    tool_f1: float
    invalid_json: float
    latency_p50: int
    latency_p95: int
    per_language: dict[str, float] = Field(default_factory=dict)
    per_intent: dict[str, float] = Field(default_factory=dict)


class Benchmark(BaseModel):
    """The last recorded routing benchmark, as the benchmark itself wrote it."""

    generated_at: str | None = None
    split: str | None = None
    prompt: str | None = None
    examples: int = 0
    rows: list[BenchmarkRow] = Field(default_factory=list)


class ModelsResponse(BaseModel):
    models: list[ModelSummary]
    router: RouterSummary
    benchmark: Benchmark | None = Field(
        default=None,
        description="Null when no benchmark report has been written on this checkout.",
    )


class RouteRequest(BaseModel):
    # Stripped before the length check, so a question of three spaces is refused rather than
    # routed. The keyword table answers GENERAL_QUESTION for it and the model invents something;
    # neither is an answer anybody typed a question to get.
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    service_hint: str | None = None


class RouteAnswer(BaseModel):
    """What one router made of the question."""

    router: str
    intent: str
    target_service: str | None
    requires_rag: bool
    requires_mcp: bool
    tools: list[str]
    latency_ms: int


class RouteComparison(BaseModel):
    query: str
    answers: list[RouteAnswer]
    agree: bool = Field(description="Whether both routers chose the same intent.")


# ------------------------------------------------------------------ dependencies ----


def get_settings() -> Settings:
    return settings()


def get_router(config: Annotated[Settings, Depends(get_settings)]) -> RouterProtocol:
    return build_router(config)


# ---------------------------------------------------------------------- endpoints ----


@router.get("", response_model=ModelsResponse)
async def list_models(config: Annotated[Settings, Depends(get_settings)]) -> ModelsResponse:
    """What runs where, checked against Ollama rather than described from settings.

    Availability is a live question and the answer changes without this service restarting: a
    model can be deleted, and Ollama can be stopped. Reporting the configured name alone would
    make a page that is confidently wrong exactly when somebody is looking at it to find out why
    routing got worse.
    """
    summaries = [
        await _summary("reasoning", config.llm_model, config),
        await _summary("embedding", config.embedding_model, config),
    ]

    if config.router_use_model:
        summaries.insert(1, await _summary("routing", config.router_model, config))

    return ModelsResponse(
        models=summaries,
        router=_router_summary(config),
        benchmark=read_benchmark(BENCHMARK_REPORT),
    )


@router.post("/route", response_model=RouteComparison)
async def compare_routes(
    request: RouteRequest,
    configured: Annotated[RouterProtocol, Depends(get_router)],
) -> RouteComparison:
    """Route one question with the shipped router and with the keyword table.

    The keyword table is asked even when it is what the shipped router already is, because the
    comparison is the point of the page and a missing second column reads as a failure rather
    than as a configuration.
    """
    answers = [await _answer(configured, request)]

    if not isinstance(configured, RuleBasedRouter):
        answers.append(await _answer(RuleBasedRouter(), request))

    return RouteComparison(
        query=request.query,
        answers=answers,
        agree=len({answer.intent for answer in answers}) == 1,
    )


# ------------------------------------------------------------------------ helpers ----


async def _answer(chosen: RouterProtocol, request: RouteRequest) -> RouteAnswer:
    started = time.perf_counter()

    try:
        decision = await chosen.route(request.query, service_hint=request.service_hint)
    except RouterError as exc:
        # Only reachable for a router with no fallback, which the shipped one has. 503 rather
        # than 500: the request was fine and the dependency was not.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return _as_answer(chosen.name, decision, round((time.perf_counter() - started) * 1000))


def _as_answer(name: str, decision: RouteDecision, latency_ms: int) -> RouteAnswer:
    return RouteAnswer(
        router=name,
        intent=decision.intent.value,
        target_service=decision.target_service,
        requires_rag=decision.requires_rag,
        requires_mcp=decision.requires_mcp,
        tools=decision.tools,
        latency_ms=latency_ms,
    )


async def probe(name: str, config: Settings) -> bool:
    """Whether Ollama has this model right now.

    A function rather than an inline call so a test can answer it without a runtime: every other
    part of this endpoint is pure, and a suite that needs Ollama up to assert the shape of a JSON
    body is a suite that fails for reasons it is not about.
    """
    provider = OllamaLlmProvider(
        base_url=config.ollama_base_url,
        model=name,
        timeout_seconds=config.router_timeout_seconds,
    )

    return await provider.is_available()


async def _summary(
    purpose: Literal["reasoning", "routing", "embedding"],
    name: str,
    config: Settings,
) -> ModelSummary:
    available = await probe(name, config)

    return ModelSummary(
        purpose=purpose,
        name=name,
        available=available,
        detail=_detail(purpose, name, available),
    )


def _detail(purpose: str, name: str, available: bool) -> str:
    if available:
        return "Loaded and reachable."

    # The router's model is built from this repository rather than pulled, so telling somebody to
    # `ollama pull sentinel-router` would send them looking for a model that does not exist
    # anywhere to pull it from.
    if purpose == "routing":
        return f"Not imported. Run: ollama create {name} -f models/sentinel-router/Modelfile"

    return f"Not available. Run: ollama pull {name}"


def _router_summary(config: Settings) -> RouterSummary:
    if not config.router_use_model:
        return RouterSummary(active="rule", model=None)

    return RouterSummary(
        active="fine_tuned",
        model=config.router_model,
        prompt_version=config.router_prompt_version,
        timeout_seconds=config.router_timeout_seconds,
        fallback="rule",
    )


def read_benchmark(path: Path) -> Benchmark | None:
    """The benchmark's own report, or nothing.

    A malformed or missing file is not an error here. The page's job is to say what has been
    measured on this checkout, and "nothing has" is a true and useful answer — where inventing a
    table, or failing the whole endpoint because one file is unreadable, are neither.
    """
    if not path.exists():
        return None

    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("%s exists but could not be read as a benchmark report", path)

        return None

    rows = [BenchmarkRow(**row) for row in payload.get("scores", []) if isinstance(row, dict)]

    return Benchmark(
        generated_at=payload.get("generated_at"),
        split=payload.get("split"),
        prompt=payload.get("prompt"),
        examples=payload.get("examples", 0),
        rows=rows,
    )
