"""Running an approved action, and the one result this must never produce.

A green verdict nobody measured is the outcome that closes an incident which is still happening.
So most of what is tested here is the `unknown` path: no service, no metric, a service that has
stopped answering Prometheus entirely. None of those is a pass, and `confirmed` stays false
through every one of them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agents.remediation import (
    ERROR_RATE_TOOL,
    HEALTHY_ERROR_RATE,
    SMOKE_TOOL,
    Remediator,
    Verdict,
)
from mcp_client.client import ToolCallResult

RESTART = "docker-mcp/restart_container"
ARGUMENTS = {"name": "sentinel-orders"}


class FakeClient:
    """Answers tool calls from a script and records what it was asked, in order."""

    def __init__(
        self,
        rates: list[float | None],
        *,
        action: ToolCallResult | None = None,
        smoke: bool | None = True,
    ) -> None:
        self._rates = list(rates)
        self._action = action
        self._smoke = smoke
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []

    async def call(self, tool: str, arguments=None, approval_token=None):  # noqa: ANN001, ANN201
        self.calls.append((tool, dict(arguments or {}), approval_token))

        if tool == SMOKE_TOOL:
            if self._smoke is None:
                return ToolCallResult(
                    tool="run_smoke_check",
                    server="testing-mcp",
                    arguments=arguments or {},
                    success=False,
                    latency_ms=3,
                    error="testing-mcp is unreachable.",
                )

            return ToolCallResult(
                tool="run_smoke_check",
                server="testing-mcp",
                arguments=arguments or {},
                success=True,
                latency_ms=120,
                content=json.dumps(
                    {
                        "passed": self._smoke,
                        "note": (
                            "Health and requests both pass."
                            if self._smoke
                            else "The service reports healthy and answers no requests."
                        ),
                    }
                ),
            )

        if tool == ERROR_RATE_TOOL:
            rate = self._rates.pop(0) if self._rates else None

            if rate is None:
                return ToolCallResult(
                    tool="get_error_rate",
                    server="metrics-mcp",
                    arguments=arguments or {},
                    success=False,
                    latency_ms=4,
                    error="Prometheus is not answering.",
                )

            return ToolCallResult(
                tool="get_error_rate",
                server="metrics-mcp",
                arguments=arguments or {},
                success=True,
                latency_ms=4,
                content=json.dumps({"window": "5m", "services": {"orders": rate}}),
            )

        return self._action or ToolCallResult(
            tool="restart_container",
            server="docker-mcp",
            arguments=arguments or {},
            success=True,
            latency_ms=900,
            content=json.dumps(
                {
                    "approved": True,
                    "approval": {"recommendation_id": "rec-1", "approved_by": "operator"},
                    "restarted": True,
                }
            ),
        )


def remediator(rates: list[float | None], **kwargs) -> tuple[Remediator, FakeClient]:
    client = FakeClient(rates, **kwargs)

    # No settle in tests: the wait is real in production and it is not what these assert.
    return Remediator(client, settle_seconds=0), client


async def run(remediation: Remediator, **overrides):  # noqa: ANN001, ANN201
    return await remediation.execute(
        **{
            "recommendation_id": "rec-1",
            "tool": RESTART,
            "arguments": ARGUMENTS,
            "approval_token": "a-token",
            "service": "orders",
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_the_symptom_is_measured_before_and_after() -> None:
    """The baseline is read here rather than reused: minutes passed while a person decided."""
    remediation, client = remediator([0.18, 0.002])
    execution = await run(remediation)

    assert execution.executed
    assert execution.verification.verdict is Verdict.RESOLVED
    assert execution.verification.error_rate_before == 0.18
    assert execution.verification.error_rate_after == 0.002
    assert execution.confirmed

    # Before, action, after, and a real request — in that order, with the approval on the action.
    assert [call[0] for call in client.calls] == [
        ERROR_RATE_TOOL,
        RESTART,
        ERROR_RATE_TOOL,
        SMOKE_TOOL,
    ]
    assert client.calls[1][2] == "a-token"
    assert execution.verification.smoke_passed is True


@pytest.mark.asyncio
async def test_a_fix_that_halved_the_errors_is_improved_and_not_resolved() -> None:
    """Better and not healthy: the summary says a person should look before it is closed."""
    remediation, _ = remediator([0.20, 0.08])
    execution = await run(remediation)

    assert execution.verification.verdict is Verdict.IMPROVED
    assert execution.verification.confirmed
    assert "not yet healthy" in execution.verification.summary


@pytest.mark.asyncio
async def test_a_fix_that_changed_nothing_says_so() -> None:
    remediation, _ = remediator([0.20, 0.19])
    execution = await run(remediation)

    assert execution.verification.verdict is Verdict.UNCHANGED
    assert not execution.verification.confirmed
    assert "not what was fixed" in execution.verification.summary


@pytest.mark.asyncio
async def test_a_fix_that_made_it_worse_suggests_reverting() -> None:
    remediation, _ = remediator([0.10, 0.31])
    execution = await run(remediation)

    assert execution.verification.verdict is Verdict.WORSE
    assert "reverting" in execution.verification.summary


@pytest.mark.asyncio
async def test_a_metric_that_cannot_be_read_is_unknown_rather_than_a_pass() -> None:
    """The failure this module exists to refuse: a green result nobody measured."""
    remediation, _ = remediator([0.18, None])
    execution = await run(remediation)

    assert execution.executed
    assert execution.verification.verdict is Verdict.UNKNOWN
    assert execution.verification.confirmed is False
    assert "not known" in execution.verification.summary


@pytest.mark.asyncio
async def test_a_service_prometheus_has_no_series_for_is_not_a_zero_error_rate() -> None:
    """A service that stopped answering has an unknown rate; calling it 0.0 would read as a fix."""
    remediation, client = remediator([])
    client._rates = []  # noqa: SLF001 - every read answers with a body naming another service

    async def missing(tool, arguments=None, approval_token=None):  # noqa: ANN001, ANN202
        client.calls.append((tool, dict(arguments or {}), approval_token))

        if tool == ERROR_RATE_TOOL:
            return ToolCallResult(
                tool="get_error_rate",
                server="metrics-mcp",
                arguments=arguments or {},
                success=True,
                latency_ms=3,
                content=json.dumps({"window": "5m", "services": {"payments": 0.0}}),
            )

        return ToolCallResult(
            tool="restart_container",
            server="docker-mcp",
            arguments=arguments or {},
            success=True,
            latency_ms=900,
            content="{}",
        )

    client.call = missing  # type: ignore[method-assign]
    execution = await run(remediation)

    assert execution.verification.verdict is Verdict.UNKNOWN
    assert execution.verification.error_rate_before is None


@pytest.mark.asyncio
async def test_an_action_that_did_not_run_is_not_verified() -> None:
    refused = ToolCallResult(
        tool="restart_container",
        server="docker-mcp",
        arguments=ARGUMENTS,
        success=False,
        latency_ms=2,
        error="The approval for 'docker-mcp/restart_container' is not valid.",
    )
    remediation, client = remediator([0.18], action=refused)
    execution = await run(remediation)

    assert execution.executed is False
    assert execution.confirmed is False
    assert "not valid" in execution.error
    assert execution.verification.verdict is Verdict.UNKNOWN

    # And nothing was measured afterwards: there is no after.
    assert [call[0] for call in client.calls] == [ERROR_RATE_TOOL, RESTART]


@pytest.mark.asyncio
async def test_without_a_service_the_action_runs_and_nothing_is_claimed() -> None:
    remediation, client = remediator([])
    execution = await run(remediation, service=None)

    assert execution.executed
    assert execution.verification.verdict is Verdict.UNKNOWN
    assert "No service was named" in execution.verification.summary
    assert [call[0] for call in client.calls] == [RESTART]


@pytest.mark.asyncio
async def test_who_approved_it_comes_back_from_the_tool() -> None:
    """`tool_calls.approval_id` needs it: an execution nobody can trace is one nobody approved."""
    remediation, _ = remediator([0.18, 0.001])
    execution = await run(remediation)

    assert execution.approved_by == "operator"


@pytest.mark.asyncio
async def test_the_healthy_threshold_is_a_fraction_not_a_percentage() -> None:
    """0.01 is one request in a hundred. Reading it as 1.0 would call a 1% outage resolved."""
    remediation, _ = remediator([0.5, HEALTHY_ERROR_RATE])
    execution = await run(remediation)

    assert execution.verification.verdict is Verdict.RESOLVED
    assert HEALTHY_ERROR_RATE == 0.01


@pytest.mark.asyncio
async def test_restarting_a_healthy_service_is_not_a_fix() -> None:
    """False remediation, found by running the chain against a service that was fine.

    Both rates read 0.0%, the restart worked perfectly, and the verdict was "the symptom is
    gone". Nothing had been wrong: an action on a service this metric says was healthy is
    unverifiable, not successful, and docs/planning.md §9 counts the difference.
    """
    remediation, _ = remediator([0.0, 0.0])
    execution = await run(remediation)

    assert execution.executed
    assert execution.verification.verdict is Verdict.UNKNOWN
    assert execution.confirmed is False
    assert "already 0.0%" in execution.verification.summary


@pytest.mark.asyncio
async def test_a_symptom_at_the_healthy_threshold_is_still_not_a_baseline() -> None:
    """One request in a hundred is noise in a system with retries, not something to fix."""
    remediation, _ = remediator([HEALTHY_ERROR_RATE, 0.0])

    assert (await run(remediation)).verification.verdict is Verdict.UNKNOWN


@pytest.mark.asyncio
async def test_a_service_that_answers_nothing_is_not_fixed_however_good_the_rate_looks() -> None:
    """The hole a rate alone cannot cover: no requests means no failed requests.

    An error rate is a fraction of the requests that arrived. A service answering nothing has a
    rate of zero, which reads as perfect health — so a real request is what tells "fixed" from
    "silent", and it overrides the number.
    """
    remediation, _ = remediator([0.40, 0.0], smoke=False)
    execution = await run(remediation)

    assert execution.verification.error_rate_after == 0.0
    assert execution.verification.verdict is Verdict.UNCHANGED
    assert execution.confirmed is False
    assert "not answering requests" in execution.verification.summary


@pytest.mark.asyncio
async def test_a_smoke_check_that_cannot_run_leaves_the_rate_to_speak() -> None:
    """testing-mcp being absent must not turn a measured improvement into a failure."""
    remediation, _ = remediator([0.40, 0.001], smoke=None)
    execution = await run(remediation)

    assert execution.verification.verdict is Verdict.RESOLVED
    assert execution.verification.smoke_passed is None
