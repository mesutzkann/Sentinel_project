"""The model router, and the keyword table standing behind it.

Phase 8 put a tuned 1.5B in front of `RuleBasedRouter` in the shipped agent, which makes one
question load-bearing: what the agent does when the model is unparseable, or absent. Before this
phase the answer was always a keyword guess; it must still be, because a machine that has never
imported the model would otherwise fail every investigation at its first node.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from llm.base import LlmUnavailableError
from routing.base import RouterError
from routing.factory import FINE_TUNED, build_router
from routing.model_router import ModelRouter
from routing.plans import decide
from routing.rule_router import RuleBasedRouter
from routing.schema import Intent
from tests.support import ScriptedProvider


def answer(intent: str, service: str | None = "payments", **overrides: object) -> str:
    body = {
        "intent": intent,
        "requires_rag": True,
        "requires_mcp": True,
        "tools": ["logs-mcp/get_service_logs"],
        "target_service": service,
    }

    return json.dumps({**body, **overrides})


class DeadProvider(ScriptedProvider):
    """Ollama is down, or the router model was never imported on this machine."""

    async def complete(self, messages, options=None):  # type: ignore[no-untyped-def]
        raise LlmUnavailableError("model 'sentinel-router' not found")


@pytest.mark.asyncio
async def test_the_plan_comes_from_the_table_not_from_the_model() -> None:
    """Whatever the model emits for flags and tools, the agent gets what `plans.py` says."""
    provider = ScriptedProvider([answer("LOG_QUERY", tools=["made-up/tool"], requires_rag=True)])
    router = ModelRouter(provider, prompt_version="v2")

    route = await router.route("show me the payments logs")

    assert route == decide(Intent.LOG_QUERY, "payments")
    assert route.tools == ["logs-mcp/get_service_logs", "logs-mcp/search_logs"]
    assert router.invalid_json == 0
    assert router.unavailable == 0


@pytest.mark.asyncio
async def test_unparseable_output_falls_back_to_the_keyword_table() -> None:
    provider = ScriptedProvider(["not json, not even close"])
    router = ModelRouter(
        provider, prompt_version="v2", constrained=False, fallback=RuleBasedRouter()
    )

    route = await router.route("show me the last 15 minutes of payments logs")

    assert route.intent is Intent.LOG_QUERY
    assert router.invalid_json == 1
    assert router.unavailable == 0


@pytest.mark.asyncio
async def test_a_missing_model_falls_back_rather_than_failing_the_run() -> None:
    """The case that decides whether shipping a model router makes the agent worse."""
    router = ModelRouter(DeadProvider([]), prompt_version="v2", fallback=RuleBasedRouter())

    route = await router.route("show me the last 15 minutes of payments logs")

    assert route.intent is Intent.LOG_QUERY
    assert route.target_service == "payments"

    # Counted apart from invalid JSON: a dead Ollama is not the model failing to produce a
    # schema, and the benchmark publishes the second number.
    assert router.unavailable == 1
    assert router.invalid_json == 0


@pytest.mark.asyncio
async def test_without_a_fallback_the_failure_is_loud() -> None:
    """The benchmark runs the model alone, and there a silent keyword guess would be a lie."""
    router = ModelRouter(DeadProvider([]), prompt_version="v2")

    with pytest.raises(RouterError):
        await router.route("show me the payments logs")


@pytest.mark.asyncio
async def test_the_service_hint_fills_a_gap_rather_than_overriding_an_answer() -> None:
    provider = ScriptedProvider(
        [answer("LOG_QUERY", service=None), answer("LOG_QUERY", service="orders")]
    )
    router = ModelRouter(provider, prompt_version="v2")

    filled = await router.route("show me the logs", service_hint="gateway")
    answered = await router.route("show me the orders logs", service_hint="gateway")

    assert filled.target_service == "gateway"
    assert answered.target_service == "orders"

    # The hint is applied afterwards, never put in the prompt: a router told the answer would
    # score well and teach us nothing.
    assert "gateway" not in provider.conversations[0][0].content


def test_the_factory_puts_the_model_in_front_of_the_table() -> None:
    built = build_router(Settings(router_use_model=True, router_model="sentinel-router"))

    assert isinstance(built, ModelRouter)
    assert built.name == FINE_TUNED


def test_the_factory_can_be_told_to_route_by_keyword_alone() -> None:
    """For a machine without the tuned model, and for measuring the agent without it."""
    built = build_router(Settings(router_use_model=False))

    assert isinstance(built, RuleBasedRouter)


def test_the_router_gets_its_own_model_and_timeout() -> None:
    """Sharing the investigation's provider would point a 1.5B router at the 7B reasoning model."""
    config = Settings(
        llm_model="qwen2.5:7b-instruct",
        llm_timeout_seconds=300.0,
        router_model="sentinel-router",
        router_timeout_seconds=20.0,
    )
    built = build_router(config)

    assert isinstance(built, ModelRouter)
    assert built._provider.model == "sentinel-router"
    assert built._provider._timeout == 20.0
