"""The agent benchmark's scoring, and the number it exists to produce.

False remediation is the one worth being afraid of: a wrong conclusion is a bad answer, and a
wrong conclusion with an executable fix attached is a bad answer somebody can click. Most of what
is tested here is the edge of that definition — what counts, what does not, and what it is
measured over.
"""

from __future__ import annotations

import json

import pytest

from evaluation.agent_eval import (
    SCENARIO_PORTS,
    AgentEvalError,
    CaseOutcome,
    _coverage,
    load_cases,
    report,
    score,
)


def outcome(**overrides) -> CaseOutcome:
    base = {
        "case": "R01",
        "scenario": "DB_CONNECTION_POOL_EXHAUSTION",
        "service": "orders",
        "final_state": "COMPLETED",
        "root_cause": "The pool was cut to 20",
        "category": "DB_CONNECTION_POOL_EXHAUSTION",
        "confidence": 0.79,
        "correct": True,
        "completed": True,
        "recommended": 1,
        "executable_recommendations": 1,
        "false_remediation": False,
        "tool_coverage": 0.8,
        "duration_s": 120.0,
    }

    return CaseOutcome(**{**base, **overrides})


def test_the_cases_come_from_the_reasoning_fixtures() -> None:
    """Reused rather than copied: a second ground truth would be a second right answer."""
    cases = load_cases(json_dir := _fixtures())

    assert {case.id for case in cases} == {
        "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08",
        "R09", "R10", "R11", "R12", "R13", "R14", "R15",
    }
    assert all(case.expected_category and case.service and case.query for case in cases)

    # The tools a good investigation reached for, taken from the recorded evidence rather than
    # from a hand-written wish list.
    assert any("database-mcp" in tool for case in cases for tool in case.expected_tools)
    assert json_dir.is_dir()


def test_asking_for_a_case_that_is_not_there_says_so() -> None:
    with pytest.raises(AgentEvalError):
        load_cases(_fixtures(), ["R99"])


def test_a_case_can_be_chosen_by_scenario_code() -> None:
    cases = load_cases(_fixtures(), ["DB_DEADLOCK"])

    assert [case.id for case in cases] == ["R03"]


def test_a_wrong_conclusion_with_a_fix_attached_is_a_false_remediation() -> None:
    scores = score([outcome(correct=False, category="MEMORY_LEAK", false_remediation=True)])

    assert scores.false_remediation_rate == 1.0
    assert scores.root_cause_accuracy == 0.0


def test_a_wrong_conclusion_with_no_executable_fix_is_not_one() -> None:
    """Advisory recommendations exist. "Add an alert" is wrong here and it is not dangerous."""
    scores = score(
        [outcome(correct=False, category="MEMORY_LEAK", executable_recommendations=0)]
    )

    assert scores.false_remediation_rate == 0.0


def test_the_rate_is_over_the_runs_that_concluded_something() -> None:
    """A run with no conclusion cannot have proposed a fix for a wrong one.

    Counting it in the denominator would flatter the number: a system that answers nothing would
    score a perfect false remediation rate.
    """
    scores = score(
        [
            outcome(correct=False, false_remediation=True),
            outcome(root_cause=None, category=None, correct=False, completed=False),
        ]
    )

    assert scores.cases == 2
    assert scores.false_remediation_rate == 1.0  # one of one conclusion, not one of two runs


def test_a_case_that_fell_over_is_not_scored_as_a_wrong_answer() -> None:
    scores = score([outcome(), outcome(case="R02", error="ConnectError: chaos API unreachable")])

    assert scores.cases == 1
    assert scores.root_cause_accuracy == 1.0


def test_tool_coverage_is_the_share_of_the_expected_tools_that_were_called() -> None:
    expected = ("logs-mcp/get_recent_errors", "database-mcp/get_connection_count")

    assert _coverage(expected, ["logs-mcp/get_recent_errors"]) == 0.5
    assert _coverage(expected, [*expected, "git-mcp/get_recent_commits"]) == 1.0
    assert _coverage((), ["anything"]) == 0.0


def test_the_report_names_the_false_remediation_rate() -> None:
    """It is the number the phase asks for; a table that buried it would be the wrong table."""
    outcomes = [outcome(correct=False, false_remediation=True)]
    table = report(outcomes, score(outcomes))

    assert "false remediation rate" in table
    assert "root cause accuracy" in table


def _fixtures():  # noqa: ANN202
    from evaluation.agent_eval import DEFAULT_CASES

    return DEFAULT_CASES


def test_every_fixture_names_a_scenario_the_services_own() -> None:
    """A scenario enabled on the wrong service is a 404 and five confusing failures."""
    from evaluation.agent_eval import SCENARIO_PORTS

    for case in load_cases(_fixtures()):
        assert case.service in SCENARIO_PORTS


def test_the_fixtures_on_disk_are_still_the_shape_this_reads() -> None:
    """Guards the reuse: the reasoning benchmark owns these files and could change them."""
    for path in _fixtures().glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))

        assert {"id", "scenario", "expected_category", "service", "query"} <= set(raw)


@pytest.mark.asyncio
async def test_a_fault_that_did_not_reproduce_is_not_scored_as_a_wrong_answer(monkeypatch) -> None:  # noqa: ANN001
    """Found the hard way: 2366 requests under an enabled scenario, not one of them failed.

    Below the knee the pool is busy rather than exhausted, the telemetry shows a slow but healthy
    service, and the agent correctly investigates an incident that is not happening. Scoring that
    as a wrong answer measures nothing — the same mistake as calling a healthy service "fixed".
    """
    from evaluation import agent_eval

    # The same rate in both windows: the service kept serving exactly as it had been.
    async def healthy(client, seconds, recipe):  # noqa: ANN001, ANN202
        return 100 * seconds, 0, 10.0

    async def nothing(client, service, path):  # noqa: ANN001, ANN202
        return None

    async def unreachable(case, config, outcome):  # noqa: ANN001, ANN202
        raise AssertionError("the agent must not be asked about an incident that did not happen")

    monkeypatch.setattr(agent_eval, "drive", healthy)
    monkeypatch.setattr(agent_eval, "chaos", nothing)
    monkeypatch.setattr(agent_eval, "investigate", unreachable)

    # A clock rather than the real one, because the duration this asserts is otherwise the
    # machine's speed. With the load and chaos calls mocked out, what is left is two connection
    # attempts to a docker-mcp that is not running: this workstation takes 2.5 s to refuse each
    # one and a Linux runner refuses both instantly, so the same case measured 5 s here and
    # under 50 ms in CI — where it rounded to 0.0 and failed. Four milliseconds is the
    # interesting number to pin: it is a real span that one decimal place cannot represent.
    ticks = iter([0.0])

    def clock() -> float:
        return next(ticks, 0.004)

    monkeypatch.setattr(agent_eval.time, "perf_counter", clock)

    case = load_cases(_fixtures(), ["R01"])[0]
    result = await agent_eval.run_case(case, object(), skip_load=False, settle=0)

    assert result.error is not None
    assert "did not reproduce" in result.error
    assert result.load["chaos_per_second"] == result.load["baseline_per_second"]
    assert result.duration_s == 0.004, "the row still records how long it took to find that out"

    # And it is excluded from accuracy rather than counted against it.
    assert score([result]).cases == 0


def test_the_load_is_the_measured_one() -> None:
    """`scripts/demo.py` measured the knee: 60 concurrent fails nothing, 120 fails 37%."""
    from evaluation.agent_eval import CHAOS_SECONDS, CONCURRENCY

    assert CONCURRENCY >= 120
    assert CHAOS_SECONDS >= 60


def test_every_case_has_load_that_reaches_its_fault() -> None:
    """The load has to hit the path the fault lives on, not merely the service that owns it.

    Driving the gateway's checkout put 6413 requests through a healthy path and failed none of
    them while the pool scenario was enabled on orders; driving the *list* endpoint then left
    the deadlock, the n+1 and the null reference untouched, because all three are behind other
    routes.
    """
    from evaluation.agent_eval import LOAD_RECIPES, LOAD_TARGETS

    # Who reaches whom, for the one scenario that has to be driven from upstream. A cascade is
    # defined by three services being slow at once, so DOWNSTREAM_LATENCY_CASCADE is driven at
    # the gateway even though payments owns it — hitting payments directly would reproduce the
    # latency and delete the thing being measured. Spelled out rather than allowed by loosening
    # the assertion, because "the load has to reach the fault" is the rule this test exists for.
    reaches = {
        "gateway": {"gateway", "users", "orders", "payments", "notifications"},
        "orders": {"orders", "payments", "notifications"},
        "payments": {"payments", "notifications"},
    }

    for case in load_cases(_fixtures()):
        recipe = LOAD_RECIPES.get(case.scenario)

        if recipe is None:
            # The default: the owning service's own list endpoint.
            assert case.service in LOAD_TARGETS
            assert case.service in LOAD_TARGETS[case.service]
            continue

        driven = next(
            (service for service, port in SCENARIO_PORTS.items() if str(port) in recipe.url),
            None,
        )

        assert driven is not None, f"{case.scenario}'s recipe names no known service"
        assert case.service in reaches.get(driven, {driven}), (
            f"{case.scenario}'s recipe drives {driven}, which never reaches {case.service}"
        )


def test_the_null_reference_recipe_uses_a_currency_the_service_does_not_know() -> None:
    """The scenario throws for an unmapped currency; TRY, USD and EUR are mapped.

    A recipe that paid in TRY would exercise the happy path with the fault enabled and report
    that the fault did not reproduce.
    """
    from evaluation.agent_eval import LOAD_RECIPES

    recipe = LOAD_RECIPES["NULL_REFERENCE_EXCEPTION"]
    currencies = [variant["currency"] for variant in recipe.body_variants]

    assert recipe.method == "POST"
    assert any(currency not in {"TRY", "USD", "EUR"} for currency in currencies)

    # And a mix, because the fault's signature is a subset failing. Sending the bad currency
    # every time failed 8636 of 8636 requests — a total outage, which the agent read as a stuck
    # circuit breaker, reasonably.
    assert any(currency in {"TRY", "USD", "EUR"} for currency in currencies)


def test_the_n_plus_one_recipe_uses_the_detail_route() -> None:
    """The `Include` that gets dropped is on `GET /orders/{id}`; the list keeps its own query."""
    from evaluation.agent_eval import LOAD_RECIPES

    assert "{order_id}" in LOAD_RECIPES["DB_N_PLUS_ONE_QUERY"].url


@pytest.mark.asyncio
async def test_a_recipe_that_needs_an_order_gets_a_real_one(monkeypatch) -> None:  # noqa: ANN001
    """The id is read rather than created: a write would land on a service about to be broken."""
    from evaluation import agent_eval

    async def an_order(client, items=1):  # noqa: ANN001, ANN202
        assert items >= 1

        return "0c4641f9-6e69-4d70-882d-f7667e4524eb"

    monkeypatch.setattr(agent_eval, "an_order", an_order)

    case = load_cases(_fixtures(), ["R04"])[0]
    recipe = await agent_eval.recipe_for(object(), case)

    assert recipe.url.endswith("0c4641f9-6e69-4d70-882d-f7667e4524eb")
    assert "{order_id}" not in recipe.url

    paying = await agent_eval.recipe_for(object(), load_cases(_fixtures(), ["R03"])[0])

    assert paying.body["order_id"] == "0c4641f9-6e69-4d70-882d-f7667e4524eb"

    # The variants carry it too, or two of three requests would authorise against nothing.
    mixed = await agent_eval.recipe_for(object(), load_cases(_fixtures(), ["R05"])[0])

    assert {variant["order_id"] for variant in mixed.body_variants} == {
        "0c4641f9-6e69-4d70-882d-f7667e4524eb"
    }


@pytest.mark.asyncio
async def test_a_fault_that_slows_everything_and_fails_nothing_did_reproduce(monkeypatch) -> None:  # noqa: ANN001
    """The missing-index scenario: 1373 requests in 75s against 1111 in 12, and zero errors.

    A reproduction check that only counted failures called that "did not reproduce" and skipped
    the case — a service serving a fifth of its traffic is broken by any definition a person
    would use.
    """
    from evaluation import agent_eval

    async def slow(client, seconds, recipe):  # noqa: ANN001, ANN202
        # 100/s healthy, 18/s under the fault, nothing failing.
        return (
            (100 * seconds, 0, 10.0)
            if seconds == agent_eval.BASELINE_SECONDS
            else (18 * seconds, 0, 55.0)
        )

    async def nothing(client, service, path):  # noqa: ANN001, ANN202
        return None

    asked = []

    async def record(case, config, outcome):  # noqa: ANN001, ANN202
        asked.append(case.id)
        outcome.final_state = "COMPLETED"

    monkeypatch.setattr(agent_eval, "drive", slow)
    monkeypatch.setattr(agent_eval, "chaos", nothing)
    monkeypatch.setattr(agent_eval, "investigate", record)

    case = load_cases(_fixtures(), ["R02"])[0]
    result = await agent_eval.run_case(case, object(), skip_load=False, settle=0)

    assert result.error is None
    assert asked == ["R02"], "the agent has to be asked about a service that is plainly broken"


def test_the_n_plus_one_recipe_asks_for_an_order_with_a_basket() -> None:
    """One query per line item is one query on a one-item order.

    Measured: 9523 requests went through the detail route at 127/s against a 93/s baseline with
    the scenario enabled, and the fault "did not reproduce" — because the order had one item.
    """
    from evaluation.agent_eval import LOAD_RECIPES

    assert LOAD_RECIPES["DB_N_PLUS_ONE_QUERY"].order_items >= 20


@pytest.mark.asyncio
async def test_a_fault_that_only_slows_each_request_still_counts(monkeypatch) -> None:  # noqa: ANN001
    """The n+1: forty queries instead of one, and throughput barely moves.

    Measured at 89/s against a 104/s baseline — inside the noise between two baseline runs, and
    plainly visible per request. Throughput and failures are the wrong instruments for a fault
    whose signature is query count.
    """
    from evaluation import agent_eval

    async def same_throughput_slower(client, seconds, recipe):  # noqa: ANN001, ANN202
        return (
            (100 * seconds, 0, 12.0)
            if seconds == agent_eval.BASELINE_SECONDS
            else (95 * seconds, 0, 48.0)
        )

    async def nothing(client, service, path):  # noqa: ANN001, ANN202
        return None

    asked = []

    async def record(case, config, outcome):  # noqa: ANN001, ANN202
        asked.append(case.id)

    monkeypatch.setattr(agent_eval, "drive", same_throughput_slower)
    monkeypatch.setattr(agent_eval, "chaos", nothing)
    monkeypatch.setattr(agent_eval, "recipe_for", lambda client, case: _recipe())
    monkeypatch.setattr(agent_eval, "investigate", record)

    case = load_cases(_fixtures(), ["R04"])[0]
    result = await agent_eval.run_case(case, object(), skip_load=False, settle=0)

    assert result.error is None, result.error
    assert asked == ["R04"]
    assert result.load["latency_ratio"] == 4.0


async def _recipe():  # noqa: ANN202
    from evaluation.agent_eval import LoadRecipe

    return LoadRecipe("http://localhost:8082/orders/abc")
