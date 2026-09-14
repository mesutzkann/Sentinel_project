"""Every benchmark, one command, one record each.

    python -m evaluation.suite                     # everything that can run here
    python -m evaluation.suite --only router,rag   # two of them
    python -m evaluation.suite --list              # what exists and what each one needs

docs/planning.md §11's done criterion for Phase 11 is "tek komutla tüm benchmarklar koşar,
frontend'de grafik" — one command, and the numbers on a screen. This is the command.

**It runs the evaluators rather than reimplementing them.** Each benchmark already knows how to
score itself and each writes its full run as JSON; this calls their `main()` with `--output` and
reads what they wrote. Scoring logic copied into a runner would be a second definition of recall,
and the two would disagree exactly once — in the report somebody is about to show people.

**A benchmark that cannot run is recorded as not having run.** They need different things: the
router needs Ollama and a tuned model, RAG needs PostgreSQL and an embedding model, the reasoning
comparison needs two models pulled. A suite that skipped them silently would produce a report
whose gaps look like zeros, so every skip carries the reason it skipped.

The record each run leaves is deliberately small and flat: kind, when, how long, how many cases,
and a handful of headline metrics. The evaluator's own full output is kept beside it, because the
headline is what a chart draws and the detail is what a person reads when the headline moves.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ai-service/evaluation/suite.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a run is written. One directory per suite invocation, named for when it started, so two
#: runs of the same benchmark never overwrite each other and the order on disk is chronological.
DEFAULT_RUNS_DIR = _REPO_ROOT / "datasets" / "evaluation" / "runs"


@dataclass(frozen=True, slots=True)
class Benchmark:
    """One thing that can be measured, and what it needs to run."""

    kind: str
    summary: str
    requires: str
    argv: tuple[str, ...]
    run: Callable[[list[str]], Any]
    headline: Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class BenchmarkRun:
    """What one benchmark did, in the shape a chart can read."""

    kind: str
    status: str
    started_at: str
    duration_ms: int = 0
    cases: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    detail_file: str | None = None
    error: str | None = None

    @property
    def ran(self) -> bool:
        return self.status == "completed"


@dataclass
class SuiteReport:
    """Every benchmark of one invocation."""

    run_id: str
    started_at: str
    duration_ms: int
    machine: str
    runs: list[BenchmarkRun] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {**asdict(self), "runs": [asdict(run) for run in self.runs]}


# ---------------------------------------------------------------- headline metrics ----


def router_headline(report: dict[str, Any]) -> dict[str, Any]:
    """The tuned router against whatever else the run measured.

    `scores` is a list because the benchmark compares routers; the best intent accuracy is the
    number the phase is judged on, and the keyword table is carried beside it because a model
    that beats nothing is not a result.
    """
    scores = report.get("scores") or []
    best = max(scores, key=lambda row: row.get("intent_accuracy", 0.0), default={})
    rule = next((row for row in scores if row.get("router") == "rule"), {})

    return {
        "best_router": best.get("router"),
        "intent_accuracy": best.get("intent_accuracy"),
        "service_accuracy": best.get("service_accuracy"),
        "tool_f1": best.get("tool_f1"),
        "invalid_json": best.get("invalid_json"),
        "latency_p50": best.get("latency_p50"),
        "baseline_router": rule.get("router"),
        "baseline_intent_accuracy": rule.get("intent_accuracy"),
        "split": report.get("split"),
    }


def rag_headline(report: dict[str, Any]) -> dict[str, Any]:
    scores = report.get("scores") or []
    best = max(scores, key=lambda row: row.get("recall_at_5", 0.0), default={})

    return {
        "best_retriever": best.get("retriever"),
        "recall_at_1": best.get("recall_at_1"),
        "recall_at_3": best.get("recall_at_3"),
        "recall_at_5": best.get("recall_at_5"),
        "mrr": best.get("mrr"),
        "latency_p50": best.get("latency_p50"),
        "k": report.get("k"),
    }


def agent_headline(report: dict[str, Any]) -> dict[str, Any]:
    """The end-to-end numbers, including the one worth being afraid of."""
    scores = report.get("scores") or {}

    return {
        "root_cause_accuracy": scores.get("root_cause_accuracy"),
        "completed": scores.get("completed"),
        "false_remediation_rate": scores.get("false_remediation_rate"),
        "tool_coverage": scores.get("mean_tool_coverage"),
        "mean_duration_s": scores.get("mean_duration_s"),
    }


def reasoning_headline(report: dict[str, Any]) -> dict[str, Any]:
    """Per model, because the whole point of that benchmark is the comparison."""
    models = report.get("models") or {}

    return {
        "models": {
            name: {
                "root_cause_accuracy": score.get("root_cause_accuracy"),
                "completed": score.get("completed"),
                "false_rejections": score.get("false_rejections"),
                "mean_llm_calls": score.get("mean_llm_calls"),
                "mean_duration_s": score.get("mean_duration_s"),
            }
            for name, score in models.items()
        },
    }


# ------------------------------------------------------------------- the benchmarks ----


def benchmarks() -> list[Benchmark]:
    """Imported lazily: each evaluator pulls in what it measures, and a suite that imported
    every one of them at module load would need Postgres to print `--list`."""
    from evaluation import agent_eval, rag_eval, reasoning_eval, router_eval

    return [
        Benchmark(
            kind="router",
            summary="Intent accuracy of the tuned router against the keyword table.",
            requires="Ollama, and `ollama create sentinel-router` for the tuned model.",
            argv=("--split", "test", "--models", "sentinel-router", "--prompt", "v2"),
            run=router_eval.main,
            headline=router_headline,
        ),
        Benchmark(
            kind="rag",
            summary="Recall of the four retrievers over the labelled query set.",
            requires="PostgreSQL with the corpus ingested, and the embedding model.",
            argv=(),
            run=rag_eval.main,
            headline=rag_headline,
        ),
        Benchmark(
            kind="agent",
            summary="The whole system against a real fault: break it, ask, score the conclusion.",
            requires=(
                "Everything: sample services, the observability stack, the MCP servers, "
                "PostgreSQL and Ollama."
            ),
            argv=(),
            run=agent_eval.main,
            headline=agent_headline,
        ),
        Benchmark(
            kind="reasoning",
            summary="Root-cause accuracy of the reasoning nodes, model against model.",
            requires="Ollama with each model pulled. No database and no MCP servers.",
            argv=(),
            run=reasoning_eval.main,
            headline=reasoning_headline,
        ),
    ]


# -------------------------------------------------------------------------- running ----


async def run_benchmark(
    benchmark: Benchmark,
    directory: Path,
    extra: Sequence[str] = (),
) -> BenchmarkRun:
    """One benchmark, its detail written beside the record. Never raises.

    A benchmark that fails is a fact about this machine, not a reason to abandon the other two —
    and a suite that stopped at the first missing dependency would be one nobody could run
    without the whole stack up.
    """
    detail = directory / f"{benchmark.kind}.json"
    started = time.perf_counter()
    run = BenchmarkRun(
        kind=benchmark.kind,
        status="completed",
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )

    print(f"\n=== {benchmark.kind} ===", file=sys.stderr)

    try:
        code = await benchmark.run([*benchmark.argv, *extra, "--output", str(detail)])
    except Exception as exc:  # noqa: BLE001 - a missing dependency is an outcome of the suite
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.warning("%s did not run: %s", benchmark.kind, run.error)

        return run

    run.duration_ms = int((time.perf_counter() - started) * 1000)

    if code != 0:
        run.status = "failed"
        run.error = f"the benchmark exited with {code}"

        return run

    if not detail.exists():
        run.status = "failed"
        run.error = "the benchmark reported success and wrote no output"

        return run

    report = json.loads(detail.read_text(encoding="utf-8"))
    run.metrics = benchmark.headline(report)
    run.cases = _cases(report)
    run.detail_file = detail.name

    return run


def _cases(report: dict[str, Any]) -> int:
    """How many things were measured, whatever the benchmark calls them."""
    for key in ("examples", "queries", "cases"):
        value = report.get(key)

        if isinstance(value, int):
            return value

    scores = report.get("scores") or []

    if scores and isinstance(scores[0], dict) and isinstance(scores[0].get("examples"), int):
        return scores[0]["examples"]

    outcomes = report.get("outcomes")

    return len(outcomes) if isinstance(outcomes, list) else 0


async def run_suite(
    chosen: list[Benchmark],
    runs_dir: Path = DEFAULT_RUNS_DIR,
    extra: Sequence[str] = (),
) -> SuiteReport:
    started = time.perf_counter()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = runs_dir / run_id
    directory.mkdir(parents=True, exist_ok=True)

    report = SuiteReport(
        run_id=run_id,
        started_at=datetime.now(UTC).isoformat(timespec="seconds"),
        duration_ms=0,
        machine=f"{platform.system()} {platform.machine()}",
    )

    for benchmark in chosen:
        report.runs.append(await run_benchmark(benchmark, directory, extra))

    report.duration_ms = int((time.perf_counter() - started) * 1000)
    (directory / "suite.json").write_text(
        json.dumps(report.to_payload(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return report


def render(report: SuiteReport) -> str:
    """The table a person reads in the terminal. The same numbers the page draws."""
    lines = [
        "",
        f"Suite {report.run_id} on {report.machine}, {report.duration_ms / 1000:.0f}s",
        "",
        "| benchmark | status | cases | headline |",
        "|---|---|---|---|",
    ]

    for run in report.runs:
        lines.append(
            f"| {run.kind} | {run.status} | {run.cases or '-'} | {_headline_text(run)} |"
        )

    failed = [run for run in report.runs if not run.ran]

    if failed:
        lines += ["", "Did not run:"]
        lines += [f"- **{run.kind}**: {run.error}" for run in failed]

    return "\n".join(lines)


def _headline_text(run: BenchmarkRun) -> str:
    if not run.ran:
        return "—"

    if run.kind == "router":
        return (
            f"intent {_pct(run.metrics.get('intent_accuracy'))} "
            f"against {_pct(run.metrics.get('baseline_intent_accuracy'))} for the keyword table"
        )

    if run.kind == "rag":
        return (
            f"{run.metrics.get('best_retriever')}: "
            f"R@5 {_pct(run.metrics.get('recall_at_5'))}, "
            f"R@3 {_pct(run.metrics.get('recall_at_3'))}"
        )

    if run.kind == "agent":
        return (
            f"root cause {_pct(run.metrics.get('root_cause_accuracy'))}, "
            f"false remediation {_pct(run.metrics.get('false_remediation_rate'))}"
        )

    if run.kind == "reasoning":
        models = run.metrics.get("models") or {}

        return ", ".join(
            f"{name} {_pct(score.get('root_cause_accuracy'))}" for name, score in models.items()
        ) or "—"

    return json.dumps(run.metrics)


def _pct(value: Any) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, int | float) else "—"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated benchmark kinds; default is all of them",
    )
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    parser.add_argument("--list", action="store_true", help="what exists and what it needs")
    parser.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="arguments passed through to every benchmark, after a bare --",
    )
    args = parser.parse_args(argv)
    available = benchmarks()

    if args.list:
        for benchmark in available:
            print(f"{benchmark.kind:<12} {benchmark.summary}\n{'':<12} needs: {benchmark.requires}")

        return 0

    wanted = [kind.strip() for kind in args.only.split(",") if kind.strip()]
    chosen = [b for b in available if not wanted or b.kind in wanted]

    if wanted and len(chosen) != len(wanted):
        unknown = sorted(set(wanted) - {b.kind for b in chosen})
        print(
            f"No benchmark called {', '.join(unknown)}. "
            f"There is: {', '.join(b.kind for b in available)}.",
            file=sys.stderr,
        )

        return 1

    extra = [argument for argument in args.rest if argument != "--"]
    report = await run_suite(chosen, args.runs_dir, extra)

    print(render(report))
    print(f"\nWrote {args.runs_dir / report.run_id}", file=sys.stderr)

    # Non-zero when something did not run, so a CI step fails rather than reporting a suite of
    # skips as a pass.
    return 0 if all(run.ran for run in report.runs) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(main()))
