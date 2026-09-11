"""The keyword router, and the properties the whole router interface has to keep."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from routing.plans import INTENT_PLANS
from routing.rule_router import KNOWN_SERVICES, RuleBasedRouter
from routing.schema import Intent, RouteDecision


@pytest.fixture
def router() -> RuleBasedRouter:
    return RuleBasedRouter()


def test_every_intent_has_a_plan() -> None:
    """A router that can return an intent nothing knows how to execute is a crash in waiting.

    This is the check that stops the fifteenth intent being added without deciding what the
    agent should do about it.
    """
    assert set(INTENT_PLANS) == set(Intent)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("payments servisini arastir, neden hata veriyor", Intent.FULL_INVESTIGATION),
        ("investigate why orders is failing", Intent.FULL_INVESTIGATION),
        ("show me the recent errors in payments", Intent.ERROR_ANALYSIS),
        ("orders neden yavas", Intent.PERFORMANCE_ANALYSIS),
        ("what is the p99 latency of gateway", Intent.PERFORMANCE_ANALYSIS),
        ("odeme servisinde daha once kilitlenme yasadik mi", Intent.HISTORICAL_SIMILARITY),
        ("what changed in the last deploy", Intent.DEPLOYMENT_CHECK),
        ("baglanti havuzu tukendi", Intent.DATABASE_HEALTH),
        ("hangi servis hangi servisi cagiriyor", Intent.SERVICE_TOPOLOGY),
        ("show me the logs for notifications", Intent.LOG_QUERY),
        ("get me the recent traces", Intent.TRACE_QUERY),
        # Definitional, and names no service: a runbook question, not an investigation.
        ("how do i diagnose a deadlock", Intent.KNOWLEDGE_QUESTION),
        ("loglar nerede tutuluyor ve nasil sorgulanir", Intent.KNOWLEDGE_QUESTION),
        # The same phrasing about a named service is operational again.
        ("what is the error rate of payments", Intent.ERROR_ANALYSIS),
        ("aaaa bbbb cccc", Intent.GENERAL_QUESTION),
    ],
)
async def test_it_classifies_both_languages(
    router: RuleBasedRouter, query: str, expected: Intent
) -> None:
    decision = await router.route(query)

    assert decision.intent is expected


async def test_turkish_accents_do_not_change_the_answer(router: RuleBasedRouter) -> None:
    """Someone on an English keyboard writes "arastir" and gets the same plan.

    The rules carry both spellings, and the normaliser strips accents on top, so neither the
    accented nor the unaccented form depends on the other being listed.
    """
    accented = await router.route("payments servisini araştır")
    plain = await router.route("payments servisini arastir")

    assert accented == plain
    assert accented.intent is Intent.FULL_INVESTIGATION


async def test_a_named_service_beats_the_incident_hint(router: RuleBasedRouter) -> None:
    decision = await router.route("show me the logs for notifications", service_hint="orders")

    assert decision.target_service == "notifications"


async def test_two_named_services_resolve_to_neither(router: RuleBasedRouter) -> None:
    """A cascade is not a local failure, and picking one end of it would make it look like one."""
    decision = await router.route(
        "why does orders time out calling payments", service_hint="orders"
    )

    assert decision.target_service is None


async def test_the_hint_is_used_when_the_question_names_nobody(router: RuleBasedRouter) -> None:
    decision = await router.route("why is it throwing exceptions", service_hint="payments")

    assert decision.target_service == "payments"


async def test_a_service_name_inside_a_word_does_not_count(router: RuleBasedRouter) -> None:
    decision = await router.route("the customer reorders constantly", service_hint=None)

    assert decision.target_service is None


async def test_documentation_questions_need_no_live_signal(router: RuleBasedRouter) -> None:
    """KNOWLEDGE_QUESTION and friends are answered from the corpus, so requires_mcp is False.

    It matters because the planner skips every collector on the strength of it, and an agent
    that queried Loki to answer "what is a deadlock" would be spending budget on nothing.
    """
    decision = await router.route("what is a connection pool, explain it")

    assert decision.requires_rag is True
    assert decision.requires_mcp is False
    assert decision.tools == []


async def test_the_decision_is_frozen(router: RuleBasedRouter) -> None:
    """The planner reads it repeatedly; nothing downstream should be able to edit the plan."""
    decision = await router.route("investigate orders")

    with pytest.raises(ValidationError):
        decision.intent = Intent.LOG_QUERY  # type: ignore[misc]


def test_every_suggested_tool_is_qualified() -> None:
    """``server/tool``, because a bare tool name is ambiguous across servers.

    Two servers expose ``get_error_rate`` — one over log lines, one over requests — so an
    unqualified suggestion would be unresolvable exactly where it matters.
    """
    for intent in Intent:
        _, _, tools = INTENT_PLANS[intent]

        for tool in tools:
            assert tool.count("/") == 1, f"{intent}: {tool}"
            server, name = tool.split("/")
            assert server.endswith("-mcp"), f"{intent}: {tool}"
            assert name


def test_the_known_services_are_the_sample_estate() -> None:
    assert set(KNOWN_SERVICES) == {
        "gateway",
        "users",
        "orders",
        "payments",
        "notifications",
    }


async def test_it_always_returns_a_decision(router: RuleBasedRouter) -> None:
    """Including for an empty question. The router has no failure mode short of RouterError."""
    decision = await router.route("")

    assert isinstance(decision, RouteDecision)
    assert decision.intent is Intent.GENERAL_QUESTION
