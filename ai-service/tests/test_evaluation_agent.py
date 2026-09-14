"""The agent benchmark's scoring, and the number it exists to produce.

False remediation is the one worth being afraid of: a wrong conclusion is a bad answer, and a
wrong conclusion with an executable fix attached is a bad answer somebody can click. Most of what
is tested here is the edge of that definition — what counts, what does not, and what it is
measured over.
"""

from __future__ import annotations

import json

import pytest

from evaluation.agent_eval import AgentEvalError, CaseOutcome, _coverage, load_cases, report, score


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

    assert {case.id for case in cases} == {"R01", "R02", "R03", "R04", "R05"}
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
    async def healthy(client, seconds, service):  # noqa: ANN001, ANN202
        return 100 * seconds, 0

    async def nothing(client, service, path):  # noqa: ANN001, ANN202
        return None

    async def unreachable(case, config, outcome):  # noqa: ANN001, ANN202
        raise AssertionError("the agent must not be asked about an incident that did not happen")

    monkeypatch.setattr(agent_eval, "drive", healthy)
    monkeypatch.setattr(agent_eval, "chaos", nothing)
    monkeypatch.setattr(agent_eval, "investigate", unreachable)

    case = load_cases(_fixtures(), ["R01"])[0]
    result = await agent_eval.run_case(case, object(), skip_load=False, settle=0)

    assert result.error is not None
    assert "did not reproduce" in result.error
    assert result.load["chaos_per_second"] == result.load["baseline_per_second"]
    assert result.duration_s > 0, "the row still records how long it took to find that out"

    # And it is excluded from accuracy rather than counted against it.
    assert score([result]).cases == 0


def test_the_load_is_the_measured_one() -> None:
    """`scripts/demo.py` measured the knee: 60 concurrent fails nothing, 120 fails 37%."""
    from evaluation.agent_eval import CHAOS_SECONDS, CONCURRENCY

    assert CONCURRENCY >= 120
    assert CHAOS_SECONDS >= 60


def test_every_case_has_a_load_target_on_its_own_service() -> None:
    """The load has to hit the path the fault lives on.

    Driving the gateway's checkout instead put 6413 requests through a healthy path and failed
    none of them while the pool scenario was enabled on orders.
    """
    from evaluation.agent_eval import LOAD_TARGETS

    for case in load_cases(_fixtures()):
        assert case.service in LOAD_TARGETS
        assert case.service in LOAD_TARGETS[case.service]


@pytest.mark.asyncio
async def test_a_fault_that_slows_everything_and_fails_nothing_did_reproduce(monkeypatch) -> None:  # noqa: ANN001
    """The missing-index scenario: 1373 requests in 75s against 1111 in 12, and zero errors.

    A reproduction check that only counted failures called that "did not reproduce" and skipped
    the case — a service serving a fifth of its traffic is broken by any definition a person
    would use.
    """
    from evaluation import agent_eval

    async def slow(client, seconds, service):  # noqa: ANN001, ANN202
        # 100/s healthy, 18/s under the fault, nothing failing.
        return (100 * seconds, 0) if seconds == agent_eval.BASELINE_SECONDS else (18 * seconds, 0)

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
