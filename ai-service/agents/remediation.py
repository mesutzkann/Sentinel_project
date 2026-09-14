"""Doing the approved thing, and then finding out whether it worked.

docs/planning.md §3 step 8: the approved action runs, and then the system measures. The second
half is the one that is easy to leave out and the one the phase is really about — a remediation
that reports "restarted" and stops has told you what it did, not what happened. An incident is
resolved when the symptom is gone, and the only thing that knows that is the metric that showed
the symptom in the first place.

So an execution is three measurements and one action:

1. **Before.** The error rate now, read before anything changes. Taken here rather than reused
   from the investigation because minutes have passed while a person read the recommendation and
   decided, and a baseline from before they started describes a different system.
2. **The action**, through the same MCP client and policy every other tool call goes through.
   There is no second path to a destructive tool.
3. **Settle, then after.** A service that has just restarted is not yet serving; measuring
   immediately reports the restart as a catastrophe. The window is short and explicit, and the
   result says how long it waited.

**A verification that cannot run is not a pass.** No metric, no service name, no settle — every
one of those is reported as `unknown` rather than as success. The one outcome this module must
never produce is a green result nobody measured, because that is the outcome that closes an
incident which is still happening.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from mcp_client.client import McpClient, ToolCallResult

logger = logging.getLogger(__name__)

#: How long to let a service settle before measuring it again. A restarted container answers
#: health checks within a few seconds and its error rate needs a little longer to mean anything,
#: because the rate is computed over a window that still contains the outage.
DEFAULT_SETTLE_SECONDS = 30

#: The window the before and after rates are read over. Shorter than the investigation's, on
#: purpose: a fifteen-minute window an hour into an incident is mostly the incident, and a fix
#: that worked perfectly would move it by a tenth.
VERIFY_WINDOW_MINUTES = 5

#: Below this, a service is not failing. Error rates are fractions, so 0.01 is one request in a
#: hundred — noise in a system with retries, and not a symptom anybody paged for.
HEALTHY_ERROR_RATE = 0.01

#: The fraction of the original error rate a fix has to remove to be called an improvement.
#: Half, which is deliberately modest: this says "the direction is right and the measurement is
#: real", not "the incident is over" — that judgement is a person's.
IMPROVEMENT_SHARE = 0.5

ERROR_RATE_TOOL = "metrics-mcp/get_error_rate"


class Verdict(StrEnum):
    """What the measurement says about the fix."""

    RESOLVED = "resolved"
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    WORSE = "worse"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Verification:
    """The measurement either side of the action."""

    verdict: Verdict
    summary: str
    service: str | None = None
    error_rate_before: float | None = None
    error_rate_after: float | None = None
    settled_seconds: int = 0
    window_minutes: int = VERIFY_WINDOW_MINUTES

    @property
    def confirmed(self) -> bool:
        """Only a measured improvement. `unknown` is not a pass."""
        return self.verdict in (Verdict.RESOLVED, Verdict.IMPROVED)


@dataclass(frozen=True, slots=True)
class Execution:
    """One approved action, what it did, and whether the symptom went away."""

    recommendation_id: str
    tool: str
    executed: bool
    verification: Verification
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    latency_ms: int = 0
    approved_by: str | None = None

    @property
    def confirmed(self) -> bool:
        """The action ran *and* the measurement improved.

        One definition of "it worked", here rather than in the endpoint: `executed` and
        `confirmed` are different questions, and a caller that had to remember to combine them
        would eventually not.
        """
        return self.executed and self.verification.confirmed

    def to_payload(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "verification": asdict(self.verification),
            "confirmed": self.confirmed,
        }


class Remediator:
    """Executes one approved action and verifies it."""

    def __init__(
        self,
        client: McpClient,
        *,
        settle_seconds: int = DEFAULT_SETTLE_SECONDS,
        window_minutes: int = VERIFY_WINDOW_MINUTES,
    ) -> None:
        self._client = client
        self._settle = settle_seconds
        self._window = window_minutes

    async def execute(
        self,
        *,
        recommendation_id: str,
        tool: str,
        arguments: dict[str, Any],
        approval_token: str,
        service: str | None = None,
        verify: bool = True,
    ) -> Execution:
        """Run it, then measure it.

        Never raises. A failed tool call, an unreachable server and a metric that will not answer
        are all outcomes the caller has to be able to show a person, and an exception here would
        become a 500 where the useful thing is "the restart ran and the verification could not".
        """
        before = await self._error_rate(service) if verify and service else None

        started = time.perf_counter()
        call = await self._client.call(tool, arguments, approval_token)
        latency_ms = int((time.perf_counter() - started) * 1000)

        if not call.success:
            return Execution(
                recommendation_id=recommendation_id,
                tool=tool,
                arguments=arguments,
                executed=False,
                error=call.error,
                latency_ms=latency_ms,
                verification=Verification(
                    Verdict.UNKNOWN,
                    "The action did not run, so there is nothing to verify.",
                    service=service,
                ),
            )

        approved_by = _approved_by(call)

        if not verify or not service:
            return Execution(
                recommendation_id=recommendation_id,
                tool=tool,
                arguments=arguments,
                executed=True,
                result=call.content,
                latency_ms=latency_ms,
                approved_by=approved_by,
                verification=Verification(
                    Verdict.UNKNOWN,
                    (
                        "No service was named, so the symptom was not measured."
                        if verify
                        else "Verification was not asked for."
                    ),
                    service=service,
                ),
            )

        verification = await self._verify(service, before)

        return Execution(
            recommendation_id=recommendation_id,
            tool=tool,
            arguments=arguments,
            executed=True,
            result=call.content,
            latency_ms=latency_ms,
            approved_by=approved_by,
            verification=verification,
        )

    async def _verify(self, service: str, before: float | None) -> Verification:
        """Settle, measure, and say what the two numbers mean."""
        if self._settle > 0:
            await asyncio.sleep(self._settle)

        after = await self._error_rate(service)

        if before is None or after is None:
            return Verification(
                Verdict.UNKNOWN,
                (
                    "The error rate could not be read "
                    f"{'before' if before is None else 'after'} the action, so whether it "
                    "helped is not known."
                ),
                service=service,
                error_rate_before=before,
                error_rate_after=after,
                settled_seconds=self._settle,
                window_minutes=self._window,
            )

        verdict, summary = _judge(before, after)

        return Verification(
            verdict,
            summary,
            service=service,
            error_rate_before=before,
            error_rate_after=after,
            settled_seconds=self._settle,
            window_minutes=self._window,
        )

    async def _error_rate(self, service: str | None) -> float | None:
        """The service's error rate as a fraction, or None when it cannot be read."""
        if not service:
            return None

        call = await self._client.call(
            ERROR_RATE_TOOL, {"service": service, "minutes": self._window}
        )

        if not call.success:
            logger.warning("Could not read the error rate for %s: %s", service, call.error)

            return None

        content = _as_dict(call.content)
        rates = content.get("services")

        if not isinstance(rates, dict) or service not in rates:
            # Prometheus answers with no series for a service that is not serving at all, which
            # is a real state and not a rate. Reported as unreadable rather than as zero: a
            # service that has stopped answering has an error rate of "unknown", and calling it
            # 0.0 would read as a fix.
            return None

        try:
            return float(rates[service])
        except (TypeError, ValueError):
            return None


def _judge(before: float, after: float) -> tuple[Verdict, str]:
    """What two error rates say about what happened in between."""
    if before <= HEALTHY_ERROR_RATE:
        # Nothing was wrong in this metric before the action, so nothing in it can show the
        # action helped. Found by running the chain against a healthy service: it restarted
        # cleanly, both rates read 0.0%, and the verdict was "the symptom is gone" — a false
        # remediation, which is the one docs/planning.md §9 names as a number worth measuring.
        # An action on a service this metric says was fine is unverifiable, not successful.
        return (
            Verdict.UNKNOWN,
            f"The error rate was already {_pct(before)} before the action, so this measurement "
            "cannot show whether it helped. Either the symptom is not in the error rate, or "
            "there was no symptom.",
        )

    if after <= HEALTHY_ERROR_RATE:
        return (
            Verdict.RESOLVED,
            f"The error rate went from {_pct(before)} to {_pct(after)}: the symptom is gone.",
        )

    if after <= before * IMPROVEMENT_SHARE:
        return (
            Verdict.IMPROVED,
            f"The error rate fell from {_pct(before)} to {_pct(after)} — better, and not yet "
            "healthy. Somebody should look before this is called resolved.",
        )

    if after > before:
        return (
            Verdict.WORSE,
            f"The error rate rose from {_pct(before)} to {_pct(after)}. The action did not help "
            "and may have hurt; consider reverting it.",
        )

    return (
        Verdict.UNCHANGED,
        f"The error rate is {_pct(after)} against {_pct(before)} before: no measurable change. "
        "The cause is probably not what was fixed.",
    )


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _as_dict(content: Any) -> dict[str, Any]:
    """Tool content, whether it arrived as an object or as a JSON string."""
    if isinstance(content, dict):
        return content

    if isinstance(content, str):
        import json

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return {}

        return parsed if isinstance(parsed, dict) else {}

    return {}


def _approved_by(call: ToolCallResult) -> str | None:
    """Who approved it, as the destructive tool reported it back."""
    approval = _as_dict(call.content).get("approval")

    return approval.get("approved_by") if isinstance(approval, dict) else None
