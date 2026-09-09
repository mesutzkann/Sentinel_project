"""The five collectors that call MCP servers, and what each one decides a result is worth.

The servers share a convention that makes this short: a tool that looks for instances returns
``{found, window, items, note?, interpretation?}``, and a tool that measures returns the
measurement. So a collector's whole job is to say which tools apply, and to weigh what comes
back.

**On weights, and the thresholds that are deliberately absent.** The weight feeds the
evidence-support term of the confidence score, and it answers one question: how much does this
fact narrow the answer. Three values cover it:

* ``0.8`` — the tool looked for instances and found some.
* ``0.4`` — it looked and found none. That is real evidence and it is what separates a slow query
  with no errors from a pool exhaustion with many, but a negative supports fewer hypotheses than
  a positive does.
* ``0.6`` — a gauge, which always returns a number.

What is not here is a table of thresholds deciding that a 5 ms p99 is fine and a 500 ms one is
not. Inventing those would put the diagnosis in the collector, where it would be invisible, and
the reasoning nodes see the numbers themselves. The single exception is connection saturation:
``get_connection_count`` returns ``headroom``, so "there is none left" is something the server
measured rather than something this module decided.
"""

from __future__ import annotations

from typing import Any

from agents.context import EvidenceItem, EvidenceSource, InvestigationContext
from agents.nodes.collect import CollectorNode, ToolRequest
from agents.states import State
from mcp_client.client import ToolCallResult

WEIGHT_POSITIVE = 0.8
WEIGHT_NEGATIVE = 0.4
WEIGHT_GAUGE = 0.6
WEIGHT_SATURATED = 0.9

# How much of a server's own prose to carry into the summary. The `interpretation` fields are
# written for exactly this purpose and are a sentence or two; the cap is against a future one
# that is not.
_INTERPRETATION_CHARS = 240


def _counted(
    result: ToolCallResult,
    source: EvidenceSource,
    label: str,
    *,
    items_key: str = "items",
) -> EvidenceItem | None:
    """Summarise a ``{found, items, ...}`` result, which is most of them."""
    content = result.content

    if not isinstance(content, dict):
        return None

    found = content.get("found")

    if found is None:
        found = len(content.get(items_key) or [])

    window = content.get("window", "the window")
    summary = f"{label}: {found} in {window}"

    detail = content.get("interpretation") or content.get("note")

    if detail:
        summary = f"{summary} — {str(detail)[:_INTERPRETATION_CHARS]}"

    return EvidenceItem(
        source=source,
        summary=summary,
        weight=WEIGHT_POSITIVE if found else WEIGHT_NEGATIVE,
        raw=content,
        tool=f"{result.server}/{result.tool}",
    )


class CollectLogsNode(CollectorNode):
    """Errors and the exception shapes behind them."""

    source = EvidenceSource.LOGS

    @property
    def state(self) -> State:
        return State.COLLECT_LOGS

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        service = ctx.target_service

        if service is None:
            # Both log tools require a service. Asking Loki for "everything" over a five-service
            # estate returns a volume no summary survives, and a wrong guess at the service is
            # worse than a step that says it had none.
            return []

        return [
            ToolRequest(
                "logs-mcp/get_recent_errors",
                {"service": service, "minutes": ctx.window_minutes, "limit": 20},
            ),
            ToolRequest(
                "logs-mcp/get_exception_statistics",
                {"service": service, "minutes": ctx.window_minutes},
            ),
        ]

    def skipped_reason(self, ctx: InvestigationContext) -> str:
        return "no single service to read logs for, so the log collector was skipped"

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        label = "recent errors" if result.tool == "get_recent_errors" else "exception types"

        return _counted(result, self.source, label)


class CollectMetricsNode(CollectorNode):
    """Error rate and response time, which between them place most failures.

    Both tools take an optional service, so unlike the log collector this one still works when
    the question named nobody — it comes back with every service's numbers, which is what a
    cascade looks like from the outside.
    """

    source = EvidenceSource.METRICS

    @property
    def state(self) -> State:
        return State.COLLECT_METRICS

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        arguments: dict[str, Any] = {"minutes": ctx.window_minutes}

        if ctx.target_service:
            arguments["service"] = ctx.target_service

        return [
            ToolRequest("metrics-mcp/get_error_rate", dict(arguments)),
            ToolRequest("metrics-mcp/get_response_time", dict(arguments)),
        ]

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        content = result.content

        if not isinstance(content, dict):
            return None

        services = content.get("services") or {}
        window = content.get("window", "the window")

        if result.tool == "get_error_rate":
            readings = ", ".join(f"{name} {value}" for name, value in services.items())
            summary = f"error rate in {window}: {readings or 'no services reported'}"
        else:
            unit = content.get("unit", "")
            readings = ", ".join(
                f"{name} p50={_at(stats, 'p50')} p95={_at(stats, 'p95')} p99={_at(stats, 'p99')}"
                for name, stats in services.items()
            )
            summary = f"response time in {window} ({unit}): {readings or 'no services reported'}"

        return EvidenceItem(
            source=self.source,
            summary=summary,
            weight=WEIGHT_GAUGE,
            raw=content,
            tool=f"{result.server}/{result.tool}",
        )


class CollectTracesNode(CollectorNode):
    """Failed spans and slow ones — or, with no service in the question, the call graph.

    The swap is not a fallback. A question with no single service is usually about how the
    services relate ("which service calls which", a cascade), and the dependency edges answer
    that where per-service span lists cannot.
    """

    source = EvidenceSource.TRACES

    @property
    def state(self) -> State:
        return State.COLLECT_TRACES

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        service = ctx.target_service

        if service is None:
            return [ToolRequest("traces-mcp/get_service_dependencies", {})]

        return [
            ToolRequest(
                "traces-mcp/get_failed_traces",
                {"service": service, "minutes": ctx.window_minutes, "limit": 10},
            ),
            ToolRequest(
                "traces-mcp/get_slowest_spans",
                {"service": service, "minutes": ctx.window_minutes, "limit": 10},
            ),
        ]

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        if result.tool == "get_service_dependencies":
            return _counted(result, self.source, "dependency edges", items_key="edges")

        label = "failed traces" if result.tool == "get_failed_traces" else "slowest spans"

        return _counted(result, self.source, label)


class CollectDatabaseNode(CollectorNode):
    """Connections, locks and slow statements — the discriminators for four of the scenarios.

    None of its tools takes a service: there is one PostgreSQL behind the whole estate, and the
    question "is the database the problem" is not per-service even when the symptom is.
    """

    source = EvidenceSource.DATABASE

    @property
    def state(self) -> State:
        return State.COLLECT_DATABASE

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        return [
            ToolRequest("database-mcp/get_connection_count", {}),
            ToolRequest("database-mcp/get_locks_and_deadlocks", {}),
            ToolRequest("database-mcp/get_slow_queries", {"limit": 10}),
        ]

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        content = result.content

        if not isinstance(content, dict):
            return None

        tool = f"{result.server}/{result.tool}"

        if result.tool == "get_connection_count":
            used = content.get("used")
            cap = content.get("max_connections")
            headroom = content.get("headroom")
            summary = f"database connections: {used} of {cap} used, {headroom} spare"

            note = content.get("note")

            if note:
                summary = f"{summary} — {str(note)[:_INTERPRETATION_CHARS]}"

            return EvidenceItem(
                source=self.source,
                summary=summary,
                # Saturation is measured rather than judged: the server returns the headroom.
                weight=WEIGHT_SATURATED if headroom == 0 else WEIGHT_GAUGE,
                raw=content,
                tool=tool,
            )

        if result.tool == "get_locks_and_deadlocks":
            deadlocks = content.get("deadlocks_since_reset", 0)
            waits = len(content.get("current_lock_waits") or [])
            summary = f"deadlocks since reset: {deadlocks}; sessions blocked now: {waits}"

            interpretation = content.get("interpretation")

            if interpretation:
                summary = f"{summary} — {str(interpretation)[:_INTERPRETATION_CHARS]}"

            return EvidenceItem(
                source=self.source,
                summary=summary,
                weight=WEIGHT_POSITIVE if (deadlocks or waits) else WEIGHT_NEGATIVE,
                raw=content,
                tool=tool,
            )

        return _counted(result, self.source, "slow statements")


class CheckDeploymentsNode(CollectorNode):
    """What changed, and when, so a failure can be correlated against a deploy.

    The window is the investigation's, not a fixed number of commits: "the last five commits" and
    "the commits since the incident started" are different questions, and only the second one can
    exonerate a deploy.

    **No ``repo`` argument, and the first version of this had one.** It passed the service name,
    on the assumption that git-mcp keyed repositories by service. It does not: ``repo`` is a path
    relative to the mount root and the estate is one repository, so ``repo="orders"`` resolved to
    a directory that is not there and every deployment check failed. It failed *safely* — the
    base class turned it into a note rather than evidence, and the run carried on — which is how
    it was found rather than believed. Narrowing to a service would need commit filtering by
    path, which this tool does not offer.
    """

    source = EvidenceSource.GIT

    @property
    def state(self) -> State:
        return State.CHECK_DEPLOYMENTS

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        return [
            ToolRequest(
                "git-mcp/get_recent_commits",
                {"since_minutes": ctx.window_minutes, "limit": 20},
            )
        ]

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        item = _counted(result, self.source, "commits")

        if item is None or not isinstance(result.content, dict):
            return item

        subjects = [
            f"{c.get('short_sha')} {c.get('subject')}"
            for c in (result.content.get("items") or [])[:3]
        ]

        if not subjects:
            return item

        return EvidenceItem(
            source=item.source,
            summary=f"{item.summary} — {'; '.join(subjects)}",
            weight=item.weight,
            raw=item.raw,
            tool=item.tool,
        )


class InspectCodeNode(CollectorNode):
    """Reads source, but only for a question that was about source in the first place.

    The search term is the question itself. That is crude, and it is bounded by the fact that
    PLAN only reaches this state for CODE_LOOKUP and CONFIG_LOOKUP, where the question already
    contains the identifier being asked about. A full investigation does not come here, because
    reading code before there is a hypothesis is reading it without knowing what to look for.
    """

    source = EvidenceSource.SOURCE_CODE

    @property
    def state(self) -> State:
        return State.INSPECT_CODE

    def plan_calls(self, ctx: InvestigationContext) -> list[ToolRequest]:
        if not ctx.query.strip():
            return []

        arguments: dict[str, Any] = {
            "query": ctx.query,
            "limit": 10,
            "context_lines": 2,
        }

        if ctx.target_service:
            arguments["path_contains"] = ctx.target_service

        return [ToolRequest("source-code-mcp/search_code", arguments)]

    def skipped_reason(self, ctx: InvestigationContext) -> str:
        return "no question text to search the code for"

    def summarise(self, result: ToolCallResult) -> EvidenceItem | None:
        item = _counted(result, self.source, "code matches")

        if item is None or not isinstance(result.content, dict):
            return item

        paths = sorted({str(m.get("path")) for m in (result.content.get("items") or [])[:5]})

        if not paths:
            return item

        return EvidenceItem(
            source=item.source,
            summary=f"{item.summary} — {', '.join(paths)}",
            weight=item.weight,
            raw=item.raw,
            tool=item.tool,
        )


def _at(stats: Any, key: str) -> str:
    """One percentile, or a dash. Metrics come back missing a quantile more often than not."""
    if isinstance(stats, dict) and stats.get(key) is not None:
        return str(stats[key])

    return "-"
