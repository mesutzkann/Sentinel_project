"""The whole system against a real fault: break something, ask, and see what comes back.

    python -m evaluation.agent_eval                          # every case, load and all
    python -m evaluation.agent_eval --only R01,R03
    python -m evaluation.agent_eval --skip-load              # whatever telemetry is already there
    python -m evaluation.agent_eval --output run.json

**This is the benchmark whose numbers move for reasons that are not the model**, and that is the
point of it. `reasoning_eval` holds the evidence fixed and varies the model, which is what makes
"3B or 7B" answerable. This breaks a running service, lets the collectors gather whatever they
actually gather, and scores the conclusion — so a tool that returns a worse summary, a Loki that
is slow to flush, or a router that plans the wrong collectors all show up here. When the two
benchmarks disagree, this one is measuring the system and that one is measuring the model.

The cases are `datasets/evaluation/reasoning/*.json`, reused rather than copied: each already
names a scenario, the service that owns it, the question a person would ask and the category a
human agreed to. Their recorded evidence is ignored here — gathering it is what is being tested.
A second ground-truth file would be a second answer to "what is the right conclusion", and the
two would drift.

What is measured, in the order it matters:

*Root cause accuracy.* Did the conclusion carry the scenario's own category, from evidence the
system collected itself.

***False remediation.*** A run that concluded wrongly and still proposed an executable action.
This is the number docs/planning.md §9 asks for and the one worth being afraid of: a wrong
conclusion is a bad answer, and a wrong conclusion with a fix attached is a bad answer somebody
can click.

*Tool coverage.* Whether the collectors the scenario is diagnosed with were actually called.

*What it cost.* Wall clock, tool calls, model calls.

**A fault that did not reproduce is not a wrong answer.** Enabling a scenario is not the same as
breaking something: below the load knee the pool is busy rather than exhausted, and the agent
correctly investigates an incident that is not happening. So the load is measured against the
baseline and a case whose service kept serving is recorded as not run, with its numbers.

Reproduction is throughput *or* failures, because not every fault fails requests: the
missing-index scenario serves everything and serves it five times slower, which is a broken
service by any definition a person would use.

**Two of the five scenarios need load this file does not yet drive.** `DB_DEADLOCK` wants
concurrent writes to payments and `NULL_REFERENCE_EXCEPTION` wants one particular currency; a
generic read against the owning service's list endpoint leaves both untouched, and they come back
as "did not reproduce" rather than as a failure of the agent. Per-scenario load recipes are the
fix and they are not written yet.

It needs the whole stack: the sample services, the observability backends, the MCP servers,
PostgreSQL with the corpus ingested, and Ollama. That is not an accident of the design; a
benchmark of the whole system that ran without the system would be measuring something else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agents.build import build_machine
from agents.context import InvestigationContext
from agents.states import State
from app.config import Settings, settings

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = _REPO_ROOT / "datasets" / "evaluation" / "reasoning"

#: Which service owns which scenario, from `sample-services/chaos/scenarios.md`. The chaos API is
#: per service and a scenario enabled on the wrong one is a 404, so this table is the difference
#: between a benchmark and five confusing failures.
SCENARIO_PORTS: dict[str, int] = {
    "gateway": 8080,
    "users": 8081,
    "orders": 8082,
    "payments": 8083,
    "notifications": 8084,
}

#: What to hammer for each owning service. **The load has to hit the path the scenario
#: instruments**, not merely the service that owns it: the pool scenario makes `GET /orders` hold
#: a connection, so driving checkouts through the gateway warms orders without exhausting
#: anything. A first version of this file drove 6413 checkouts at 150 concurrent and failed none
#: of them, because the requests were going somewhere the fault was not.
LOAD_TARGETS: dict[str, str] = {
    "gateway": "http://localhost:8080/api/services/health",
    "users": "http://localhost:8081/users",
    "orders": "http://localhost:8082/orders?limit=50",
    "payments": "http://localhost:8083/payments?limit=50",
    "notifications": "http://localhost:8084/notifications?limit=50",
}

#: Seconds of traffic before and after the fault is introduced. The "before" matters as much as
#: the "after" — a metric with no healthy baseline in its window reads as a service that was
#: always broken.
BASELINE_SECONDS = 12
CHAOS_SECONDS = 75

#: How long to wait for the OTel collector to flush. It batches, and the agent asks the
#: observability stack rather than the service: investigating before the batch lands finds a
#: healthy system and correctly concludes so.
SETTLE_SECONDS = 20

#: Below this share of the baseline request rate, the service is considered to be failing even
#: if nothing returned an error. **Not every fault fails requests**: the missing-index scenario
#: served 1373 requests in 75 seconds against 1111 in the baseline's 12 — a fifth of the
#: throughput, zero errors, and a service that is plainly broken. A reproduction check that only
#: counted failures called that "did not reproduce" and skipped the case.
COLLAPSE_SHARE = 0.6

#: Concurrent requests while driving load. **Measured, and the number this benchmark lives or
#: dies on** — `scripts/demo.py` drove orders at 60, 120 and 200 concurrent for twenty seconds
#: each and got 0%, 37% and 35% failures. The pool scenario leaves 20 connections, holds each for
#: 800 ms and gives up after 5 s, so below the knee the requests queue politely and succeed: the
#: telemetry shows a slow but healthy service and the agent correctly investigates an incident
#: that is not happening. A first version of this file used 24 and drove 2366 requests without a
#: single failure.
CONCURRENCY = 150


class AgentEvalError(RuntimeError):
    """The stack is not in a state where this benchmark means anything."""


@dataclass(frozen=True, slots=True)
class AgentCase:
    """One scenario, and the answer a human agreed to."""

    id: str
    scenario: str
    expected_category: str
    service: str
    query: str
    incident_code: str
    expected_tools: tuple[str, ...] = ()


@dataclass
class CaseOutcome:
    """What the whole system did with one broken service."""

    case: str
    scenario: str
    service: str
    final_state: str
    root_cause: str | None = None
    category: str | None = None
    confidence: float = 0.0
    correct: bool = False
    completed: bool = False
    recommended: int = 0
    executable_recommendations: int = 0

    #: What it proposed, by action code. Counts alone make a false remediation unauditable: the
    #: number says a wrong conclusion carried a runnable fix and only the codes say which fix,
    #: which is what somebody checking the claim needs.
    actions: list[str] = field(default_factory=list)
    false_remediation: bool = False
    tools_called: list[str] = field(default_factory=list)
    tool_coverage: float = 0.0
    evidence: int = 0
    llm_calls: int = 0
    duration_s: float = 0.0
    failure_reason: str | None = None
    error: str | None = None
    load: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Scores:
    """The table, over every case that ran."""

    cases: int
    root_cause_accuracy: float
    completed: float
    false_remediation_rate: float
    mean_tool_coverage: float
    mean_confidence: float
    mean_tool_calls: float
    mean_llm_calls: float
    mean_duration_s: float


# ---------------------------------------------------------------------------- cases ----


def load_cases(directory: Path, only: list[str] | None = None) -> list[AgentCase]:
    """The reasoning fixtures, read for what the agent benchmark needs from them."""
    if not directory.is_dir():
        raise AgentEvalError(f"No case directory at {directory}.")

    cases: list[AgentCase] = []

    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))

        if only and raw["id"] not in only and raw["scenario"] not in only:
            continue

        cases.append(
            AgentCase(
                id=raw["id"],
                scenario=raw["scenario"],
                expected_category=raw["expected_category"],
                service=raw["service"],
                query=raw["query"],
                incident_code=raw.get("incident_code", f"INC-{raw['id']}"),
                # The tools the recorded evidence came from: what a good investigation of this
                # fault actually reached for. Not a hand-written list, which would be a wish.
                expected_tools=tuple(
                    sorted({item["tool"] for item in raw.get("evidence", []) if item.get("tool")})
                ),
            )
        )

    if not cases:
        raise AgentEvalError(f"No cases in {directory} matched {only}.")

    return cases


# ----------------------------------------------------------------------------- load ----


async def chaos(client: httpx.AsyncClient, service: str, path: str) -> None:
    """The chaos API is per service, and enabling can take a while the first time."""
    port = SCENARIO_PORTS.get(service)

    if port is None:
        raise AgentEvalError(f"No chaos port known for '{service}'.")

    response = await client.post(f"http://localhost:{port}/chaos/{path}", timeout=120)

    if response.status_code >= 400:
        raise AgentEvalError(
            f"chaos {path} on {service} answered {response.status_code}: {response.text[:200]}"
        )


async def drive(client: httpx.AsyncClient, seconds: int, service: str) -> tuple[int, int]:
    """Hold `CONCURRENCY` requests against the scenario's own service. Returns (served, failed).

    Against the endpoint the fault lives on, not through the gateway: see `LOAD_TARGETS`. A 5xx
    counts as failed and so does a timeout — from the outside, a request that never answered and
    one that answered 500 are the same incident.
    """
    target = LOAD_TARGETS.get(service)

    if target is None:
        raise AgentEvalError(f"No load target known for '{service}'.")

    deadline = time.perf_counter() + seconds
    served = 0
    failed = 0

    async def one() -> None:
        nonlocal served, failed

        while time.perf_counter() < deadline:
            try:
                response = await client.get(target, timeout=30)
                ok = response.status_code < 500
            except httpx.HTTPError:
                ok = False

            if ok:
                served += 1
            else:
                failed += 1

    await asyncio.gather(*(one() for _ in range(CONCURRENCY)))

    return served, failed


# ------------------------------------------------------------------------- one case ----


async def run_case(
    case: AgentCase,
    config: Settings,
    *,
    skip_load: bool,
    settle: int,
) -> CaseOutcome:
    """Break it, ask, score. Never raises: a case that fell over is a row, not the end of a run."""
    outcome = CaseOutcome(
        case=case.id, scenario=case.scenario, service=case.service, final_state="NOT_RUN"
    )
    started = time.perf_counter()
    reproduced = True

    limits = httpx.Limits(
        max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY * 2
    )

    try:
        async with httpx.AsyncClient(limits=limits) as client:
            if not skip_load:
                print(f"  {case.id}: healthy traffic", file=sys.stderr)
                await chaos(client, case.service, "reset")
                served, failed = await drive(client, BASELINE_SECONDS, case.service)
                outcome.load["baseline_served"] = served
                outcome.load["baseline_failed"] = failed

                print(f"  {case.id}: enabling {case.scenario}", file=sys.stderr)
                await chaos(client, case.service, f"{case.scenario}/enable")

                served, failed = await drive(client, CHAOS_SECONDS, case.service)
                outcome.load["chaos_served"] = served
                outcome.load["chaos_failed"] = failed

                baseline_rate = outcome.load["baseline_served"] / max(BASELINE_SECONDS, 1)
                chaos_rate = served / max(CHAOS_SECONDS, 1)
                collapsed = baseline_rate > 0 and chaos_rate < baseline_rate * COLLAPSE_SHARE
                outcome.load["baseline_per_second"] = round(baseline_rate)
                outcome.load["chaos_per_second"] = round(chaos_rate)

                if failed == 0 and not collapsed:
                    # The fault was enabled and nothing broke, so there is no incident to
                    # investigate. Scoring the agent here would mark it wrong for not finding
                    # something that did not happen — the same mistake as calling a healthy
                    # service "fixed" after a restart. Recorded as not run, with the reason.
                    outcome.error = (
                        f"{case.scenario} was enabled and {served} requests all succeeded at "
                        f"{chaos_rate:.0f}/s against a baseline of {baseline_rate:.0f}/s, so the "
                        "fault did not reproduce on this path. Either the load has to reach the "
                        "endpoint the fault lives on, or this scenario needs its own recipe."
                    )
                    print(f"  {case.id}: the fault did not reproduce", file=sys.stderr)
                    reproduced = False
                else:
                    print(
                        f"  {case.id}: {failed} of {served + failed} failed and throughput is "
                        f"{chaos_rate / baseline_rate:.0%} of baseline; settling {settle}s",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(settle)

            if reproduced:
                await investigate(case, config, outcome)
    except Exception as exc:  # noqa: BLE001 - a broken case is a row in the table
        outcome.error = f"{type(exc).__name__}: {exc}"
        logger.warning("%s failed: %s", case.id, outcome.error)
    finally:
        if not skip_load:
            # Always, even after a failure: a scenario left enabled poisons every case after it.
            try:
                async with httpx.AsyncClient() as client:
                    await chaos(client, case.service, "reset")
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not reset chaos on %s: %s", case.service, exc)

    outcome.duration_s = round(time.perf_counter() - started, 1)

    return outcome


async def investigate(case: AgentCase, config: Settings, outcome: CaseOutcome) -> None:
    """One investigation, through the same composition root the service uses."""
    from app.api.investigations import InvestigationService

    service = InvestigationService(config)
    ctx = InvestigationContext(
        investigation_id=f"eval-{case.id}",
        incident_code=case.incident_code,
        query=case.query,
        service_hint=case.service,
    )

    machine = build_machine(
        provider=service._provider,  # noqa: SLF001 - the benchmark is inside the service's seams
        mcp_client=await service._mcp_client(),  # noqa: SLF001
        retriever=await service._retriever(),  # noqa: SLF001
        router=service._router,  # noqa: SLF001
        similarity=await service._similarity(),  # noqa: SLF001
    )

    result = await machine.run(ctx)

    outcome.final_state = result.final_state.value
    outcome.completed = result.final_state is State.COMPLETED
    outcome.failure_reason = result.failure_reason
    outcome.evidence = len(ctx.evidence)
    outcome.tools_called = sorted({item.tool for item in ctx.evidence if item.tool})
    outcome.tool_coverage = _coverage(case.expected_tools, outcome.tools_called)

    if ctx.root_cause is not None:
        outcome.root_cause = ctx.root_cause.title
        outcome.category = ctx.root_cause.category
        outcome.confidence = round(ctx.root_cause.confidence, 3)
        outcome.correct = ctx.root_cause.category == case.expected_category

    outcome.recommended = len(ctx.recommendations)
    outcome.actions = [item.action_code for item in ctx.recommendations]
    outcome.executable_recommendations = sum(
        1 for item in ctx.recommendations if item.tool_name
    )

    # The number this benchmark exists for. A wrong conclusion is a bad answer; a wrong conclusion
    # with an executable fix attached is a bad answer somebody can click, and the difference is
    # the whole reason for the approval gate.
    outcome.false_remediation = (
        outcome.root_cause is not None
        and not outcome.correct
        and outcome.executable_recommendations > 0
    )


def _coverage(expected: tuple[str, ...], called: list[str]) -> float:
    """Share of the tools a good investigation used that this one also reached for."""
    if not expected:
        return 0.0

    return round(len(set(expected) & set(called)) / len(expected), 3)


# ------------------------------------------------------------------------- scoring ----


def score(outcomes: list[CaseOutcome]) -> Scores:
    ran = [outcome for outcome in outcomes if outcome.error is None]
    concluded = [outcome for outcome in ran if outcome.root_cause is not None]

    return Scores(
        cases=len(ran),
        root_cause_accuracy=_share(outcome.correct for outcome in ran),
        completed=_share(outcome.completed for outcome in ran),
        # Over the runs that concluded something: a run with no conclusion cannot have proposed a
        # fix for a wrong one, and counting it as a pass would flatter the number.
        false_remediation_rate=_share(outcome.false_remediation for outcome in concluded),
        mean_tool_coverage=_mean(outcome.tool_coverage for outcome in ran),
        mean_confidence=_mean(outcome.confidence for outcome in concluded),
        mean_tool_calls=_mean(len(outcome.tools_called) for outcome in ran),
        mean_llm_calls=_mean(outcome.llm_calls for outcome in ran),
        mean_duration_s=_mean(outcome.duration_s for outcome in ran),
    )


def _share(flags) -> float:  # noqa: ANN001
    values = list(flags)

    return round(sum(1 for value in values if value) / len(values), 4) if values else 0.0


def _mean(values) -> float:  # noqa: ANN001
    numbers = list(values)

    return round(sum(numbers) / len(numbers), 2) if numbers else 0.0


def report(outcomes: list[CaseOutcome], scores: Scores) -> str:
    lines = [
        "",
        "## Per case",
        "",
        "| case | scenario | ended | category | correct | tools | conf |",
        "|---|---|---|---|---|---|---|",
    ]

    for outcome in outcomes:
        lines.append(
            f"| {outcome.case} | {outcome.scenario} | {outcome.final_state} | "
            f"{outcome.category or '—'} | {'yes' if outcome.correct else 'no'} | "
            f"{outcome.tool_coverage:.0%} | {outcome.confidence:.2f} |"
        )

    wrong = [
        outcome
        for outcome in outcomes
        if outcome.root_cause is not None and not outcome.correct
    ]

    if wrong:
        # The conclusion in words beside the code it was tagged with. A run can name the cause
        # correctly and mis-code it, and the difference between those two failures is the
        # difference between a model that cannot diagnose and a taxonomy it cannot apply.
        lines += ["", "## What the wrong ones concluded", ""]
        lines += [
            f"- **{outcome.case}**: \"{outcome.root_cause}\" tagged {outcome.category}, "
            f"expected {outcome.scenario} — proposed {', '.join(outcome.actions) or 'nothing'}"
            for outcome in wrong
        ]

    lines += [
        "",
        "## Overall",
        "",
        f"- root cause accuracy: **{scores.root_cause_accuracy:.0%}** over {scores.cases} case(s)",
        f"- reached a conclusion it stood behind: {scores.completed:.0%}",
        f"- **false remediation rate: {scores.false_remediation_rate:.0%}** "
        "(wrong conclusion, executable fix attached)",
        f"- tool coverage: {scores.mean_tool_coverage:.0%}",
        f"- mean confidence: {scores.mean_confidence:.2f}",
        f"- mean duration: {scores.mean_duration_s:.0f}s",
    ]

    broken = [outcome for outcome in outcomes if outcome.error]

    if broken:
        lines += ["", "Did not run:"]
        lines += [f"- {outcome.case}: {outcome.error}" for outcome in broken]

    return "\n".join(lines)


def as_json(outcomes: list[CaseOutcome], scores: Scores) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cases": len(outcomes),
        "scores": asdict(scores),
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--only", default="", help="comma-separated case ids or scenario codes")
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="do not touch chaos or drive traffic; investigate whatever is already there",
    )
    parser.add_argument("--settle", type=int, default=SETTLE_SECONDS)
    parser.add_argument("--output", type=Path, help="write the full run as JSON to this path")
    args = parser.parse_args(argv)

    only = [item.strip() for item in args.only.split(",") if item.strip()]

    try:
        cases = load_cases(args.cases, only)
    except AgentEvalError as exc:
        print(str(exc), file=sys.stderr)

        return 1

    config = settings()
    print(f"{len(cases)} case(s), the whole stack", file=sys.stderr)

    outcomes: list[CaseOutcome] = []

    for case in cases:
        outcomes.append(
            await run_case(case, config, skip_load=args.skip_load, settle=args.settle)
        )

    scores = score(outcomes)
    print(report(outcomes, scores))

    if args.output:
        args.output.write_text(
            json.dumps(as_json(outcomes, scores), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nWrote {args.output}", file=sys.stderr)

    # Non-zero when nothing ran, so a suite records the benchmark as failed rather than as a row
    # of zeros.
    return 0 if scores.cases else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(main()))
