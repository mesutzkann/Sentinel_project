"""The agent-model benchmark: two models, the same evidence, one table.

    python -m evaluation.reasoning_eval                          # 3B against 7B
    python -m evaluation.reasoning_eval --models qwen2.5:7b-instruct
    python -m evaluation.reasoning_eval --repeat 3 --output run.json

It needs Ollama and the models named on the command line. It needs nothing else: no MCP servers,
no PostgreSQL, no chaos scenario enabled.

**That is the point of it, and the reason it is not `agent_eval.py`.** The Phase 11 agent
benchmark triggers a real scenario, lets the collectors gather what they gather, and scores the
whole system. Its numbers move when a tool changes, when the load generator changes, when Loki is
slow. This one holds the evidence fixed — the same six or seven facts, in the exact shape the
collectors produce them — and varies only the model, which is what makes "3B or 7B" a question
with an answer. A model that gets the wrong root cause here got it wrong from evidence that
contained the right one.

What is measured, in the order it matters:

*Root cause accuracy.* Did the conclusion carry the scenario's own category. This is the number
the demo default is chosen on.

*False rejections.* How often the critic threw out a conclusion that was already correct. A
critic that rejects everything scores the same as one that never runs, and costs three more model
calls to do it.

*Whether it finished at all.* NEEDS_HUMAN is not a failure of the machinery — it is the machinery
working — but a model that reaches it on every scenario cannot carry a demo.

*What it cost.* Wall clock, model calls, prompt tokens, and how many of those calls were repair
attempts after invalid JSON. A 7B that is right and takes four minutes is a different answer from
a 7B that is right and takes forty seconds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agents.context import EvidenceItem, EvidenceSource, InvestigationContext
from agents.nodes import (
    CollectAdditionalEvidenceNode,
    GenerateHypothesesNode,
    RankHypothesesNode,
    RecommendFixNode,
    SelectRootCauseNode,
    ValidateNode,
)
from agents.state_machine import AgentEvent, EventType, StateMachine
from agents.states import COLLECTOR_STATES, State
from app.config import Settings, settings
from llm.base import LlmUnavailableError
from llm.ollama_provider import OllamaLlmProvider

# ai-service/evaluation/reasoning_eval.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CASES = _REPO_ROOT / "datasets" / "evaluation" / "reasoning"

# The two candidates the decision is between: the development default and the one that needs the
# GPU to itself. Both are on the command line so a third can be measured without editing this.
DEFAULT_MODELS = ("qwen2.5:3b-instruct", "qwen2.5:7b-instruct")


class EvalError(RuntimeError):
    """The benchmark cannot produce a comparison, so it produces nothing."""


@dataclass(frozen=True, slots=True)
class ReasoningCase:
    """One scenario's worth of evidence, and the answer a human agreed to."""

    id: str
    scenario: str
    expected_category: str
    incident_code: str
    service: str
    query: str
    evidence: list[EvidenceItem]
    discriminator: str = ""


@dataclass
class CaseOutcome:
    """What one model did with one case."""

    case_id: str
    model: str
    expected_category: str

    final_state: str = ""
    root_cause_title: str | None = None
    root_cause_category: str | None = None
    confidence: float | None = None
    validator_valid: bool | None = None
    validator_confidence: float | None = None

    top_hypothesis_category: str | None = None
    hypothesis_categories: list[str | None] = field(default_factory=list)

    # Conclusions that were already right and were thrown out anyway. Counted per rejection
    # rather than per run, because the critic gets two chances to do it.
    false_rejections: int = 0
    rejections: int = 0
    reached_correct: bool = False

    recommendations: list[str] = field(default_factory=list)

    llm_calls: int = 0
    repairs: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0

    error: str | None = None

    @property
    def category_correct(self) -> bool:
        """The conclusion the run ended with carried the scenario's own code."""
        return self.root_cause_category == self.expected_category

    @property
    def top_hypothesis_correct(self) -> bool:
        return self.top_hypothesis_category == self.expected_category

    @property
    def completed(self) -> bool:
        return self.final_state == State.COMPLETED


def load_cases(directory: Path) -> list[ReasoningCase]:
    """Read the fixtures, refusing anything that would score wrongly rather than loudly."""
    if not directory.is_dir():
        raise EvalError(f"No case directory at {directory}")

    cases: list[ReasoningCase] = []

    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))

        try:
            evidence = [
                EvidenceItem(
                    source=EvidenceSource(item["source"]),
                    summary=item["summary"],
                    weight=float(item.get("weight", 0.5)),
                    raw=item.get("raw"),
                    tool=item.get("tool"),
                )
                for item in raw["evidence"]
            ]
        except (KeyError, ValueError) as exc:
            raise EvalError(f"{path.name}: {exc}") from exc

        cases.append(
            ReasoningCase(
                id=raw["id"],
                scenario=raw["scenario"],
                expected_category=raw["expected_category"],
                incident_code=raw["incident_code"],
                service=raw["service"],
                query=raw["query"],
                evidence=evidence,
                discriminator=raw.get("discriminator", ""),
            )
        )

    if not cases:
        raise EvalError(f"No cases in {directory}")

    return cases


def build_machine(provider: OllamaLlmProvider, emit) -> StateMachine:
    """The reasoning half of the agent, with no collectors in it.

    Starting at GENERATE_HYPOTHESES is what makes the evidence fixed. The collectors are marked
    as already visited on the context, so COLLECT_ADDITIONAL_EVIDENCE finds no gap to go back
    for — with no collector nodes registered, a back-loop would transition into a state nothing
    implements, and the runner would correctly call that a defect.
    """
    return StateMachine(
        {
            State.GENERATE_HYPOTHESES: GenerateHypothesesNode(provider),
            State.COLLECT_ADDITIONAL_EVIDENCE: CollectAdditionalEvidenceNode(),
            State.RANK_HYPOTHESES: RankHypothesesNode(),
            State.SELECT_ROOT_CAUSE: SelectRootCauseNode(provider),
            State.VALIDATE: ValidateNode(provider),
            State.RECOMMEND_FIX: RecommendFixNode(provider),
        },
        emit=emit,
        start=State.GENERATE_HYPOTHESES,
    )


async def run_case(
    case: ReasoningCase,
    model: str,
    config: Settings,
    timeout_seconds: float,
) -> CaseOutcome:
    """One model, one case, one investigation."""
    outcome = CaseOutcome(case_id=case.id, model=model, expected_category=case.expected_category)
    provider = OllamaLlmProvider(config.ollama_base_url, model, timeout_seconds=timeout_seconds)

    ctx = InvestigationContext(
        investigation_id=f"eval-{case.id}",
        incident_code=case.incident_code,
        query=case.query,
        service_hint=case.service,
    )
    ctx.evidence = list(case.evidence)

    for state in COLLECTOR_STATES:
        ctx.visits[state] = 1

    # The category of the conclusion currently on the table, so that a rejection can be scored
    # against what was rejected. Taken from the event stream rather than from the context,
    # because the context no longer holds a conclusion the critic threw away.
    proposed: str | None = None

    async def emit(event: AgentEvent) -> None:
        nonlocal proposed
        payload = event.payload or {}

        if event.type is not EventType.STEP_COMPLETED:
            return

        outcome.repairs += int(payload.get("llm_retries") or 0)

        if event.state is State.SELECT_ROOT_CAUSE:
            proposed = payload.get("category")
            outcome.reached_correct = outcome.reached_correct or (
                proposed == case.expected_category
            )

        if event.state is State.VALIDATE and payload.get("valid") is False:
            outcome.rejections += 1

            if proposed == case.expected_category:
                outcome.false_rejections += 1

    started = time.perf_counter()

    try:
        result = await build_machine(provider, emit).run(ctx)
    except LlmUnavailableError as exc:
        # The runner turns this into FAILED before it can reach here, so this is the case where
        # the runtime went away between runs rather than during one.
        outcome.error = str(exc)
        outcome.duration_ms = int((time.perf_counter() - started) * 1000)

        return outcome

    # Why a run ended, kept whatever it ended as. Without it a model that timed out and a model
    # that reasoned badly are the same row in the table.
    outcome.error = result.failure_reason
    outcome.duration_ms = result.duration_ms
    outcome.final_state = str(result.final_state)
    outcome.llm_calls = ctx.llm_calls
    outcome.prompt_tokens = ctx.prompt_tokens
    outcome.completion_tokens = ctx.completion_tokens
    outcome.hypothesis_categories = [h.category for h in ctx.hypotheses]
    outcome.top_hypothesis_category = (
        ctx.hypotheses[0].category if ctx.hypotheses else None
    )
    outcome.recommendations = [r.action_code for r in ctx.recommendations]

    if ctx.root_cause is not None:
        outcome.root_cause_title = ctx.root_cause.title
        outcome.root_cause_category = ctx.root_cause.category
        outcome.confidence = ctx.root_cause.confidence
        outcome.validator_confidence = ctx.root_cause.validator_confidence
        outcome.validator_valid = (ctx.root_cause.validator_output or {}).get("valid")

    return outcome


async def run(
    cases: list[ReasoningCase],
    models: list[str],
    config: Settings,
    repeat: int,
    timeout_seconds: float,
) -> list[CaseOutcome]:
    """Every model against every case, one at a time.

    Sequential on purpose. Two 7B generations at once on one GPU measures the queue.
    """
    outcomes: list[CaseOutcome] = []

    for model in models:
        for attempt in range(repeat):
            for case in cases:
                label = f"{model} {case.id}" + (f" #{attempt + 1}" if repeat > 1 else "")
                print(f"  {label} ...", end="", flush=True, file=sys.stderr)

                outcome = await run_case(case, model, config, timeout_seconds)
                outcomes.append(outcome)

                print(
                    f" {outcome.root_cause_category or outcome.final_state}"
                    f" ({outcome.duration_ms / 1000:.0f}s)",
                    file=sys.stderr,
                )

    return outcomes


@dataclass(frozen=True, slots=True)
class ModelScores:
    """One row of the comparison table."""

    model: str
    runs: int
    root_cause_accuracy: float
    hypothesis_accuracy: float
    reached_correct: float
    completed: float
    false_rejections: int
    rejections: int
    mean_confidence: float | None
    mean_llm_calls: float
    repairs: int
    mean_duration_s: float
    mean_prompt_tokens: float


def score(outcomes: list[CaseOutcome], model: str) -> ModelScores:
    rows = [o for o in outcomes if o.model == model]
    confidences = [o.confidence for o in rows if o.confidence is not None]

    return ModelScores(
        model=model,
        runs=len(rows),
        root_cause_accuracy=_share(o.category_correct for o in rows),
        hypothesis_accuracy=_share(o.top_hypothesis_correct for o in rows),
        reached_correct=_share(o.reached_correct for o in rows),
        completed=_share(o.completed for o in rows),
        false_rejections=sum(o.false_rejections for o in rows),
        rejections=sum(o.rejections for o in rows),
        mean_confidence=statistics.fmean(confidences) if confidences else None,
        mean_llm_calls=statistics.fmean([o.llm_calls for o in rows]) if rows else 0.0,
        repairs=sum(o.repairs for o in rows),
        mean_duration_s=statistics.fmean([o.duration_ms / 1000 for o in rows]) if rows else 0.0,
        mean_prompt_tokens=statistics.fmean([o.prompt_tokens for o in rows]) if rows else 0.0,
    )


def report(outcomes: list[CaseOutcome], cases: list[ReasoningCase], models: list[str]) -> str:
    """The comparison, and then the per-case detail that explains it."""
    scores = [score(outcomes, model) for model in models]

    lines = [
        "",
        "## Model comparison",
        "",
        "| model | root cause | top hypothesis | reached it | completed | false rejections "
        "| repairs | calls | prompt tok | mean s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]

    for row in scores:
        lines.append(
            f"| {row.model} | {row.root_cause_accuracy:.2f} | {row.hypothesis_accuracy:.2f} "
            f"| {row.reached_correct:.2f} | {row.completed:.2f} "
            f"| {row.false_rejections}/{row.rejections} | {row.repairs} "
            f"| {row.mean_llm_calls:.1f} | {row.mean_prompt_tokens:.0f} "
            f"| {row.mean_duration_s:.0f} |"
        )

    lines += [
        "",
        "*root cause*: the conclusion carried the scenario's own category. *reached it*: a "
        "correct conclusion was written at least once, whether or not it survived the critic. "
        "*false rejections*: correct conclusions the critic threw out, of all rejections.",
        "",
        "## Per case",
        "",
        "| case | scenario | model | concluded | correct | confidence | end state |",
        "|---|---|---|---|---|---|---|",
    ]

    scenario_of = {case.id: case.scenario for case in cases}

    for outcome in outcomes:
        concluded = outcome.root_cause_category or "—"
        lines.append(
            f"| {outcome.case_id} | {scenario_of.get(outcome.case_id, '?')} | {outcome.model} "
            f"| {concluded} | {'yes' if outcome.category_correct else 'no'} "
            f"| {'—' if outcome.confidence is None else f'{outcome.confidence:.2f}'} "
            f"| {outcome.final_state or outcome.error} |"
        )

    lines.append("")

    return "\n".join(lines)


def as_json(outcomes: list[CaseOutcome], models: list[str]) -> dict[str, Any]:
    return {
        "models": {model: asdict(score(outcomes, model)) for model in models},
        "outcomes": [asdict(outcome) for outcome in outcomes],
    }


def _share(flags) -> float:
    values = list(flags)

    return sum(1 for flag in values if flag) / len(values) if values else 0.0


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="fixture directory")
    parser.add_argument(
        "--models",
        default=",".join(DEFAULT_MODELS),
        help="comma-separated Ollama model names to compare",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="runs per case per model; temperature is 0 but a runtime is not a promise",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900.0,
        help=(
            "seconds per model call. Well above the service default of 120: a 7B at Q4 that "
            "does not fit the GPU spends a minute a call, and a timeout scores it as broken "
            "rather than as slow, which is the one thing this benchmark must not do"
        ),
    )
    parser.add_argument("--output", type=Path, help="write the full run as JSON to this path")
    args = parser.parse_args(argv)

    config = settings()
    models = [name.strip() for name in args.models.split(",") if name.strip()]

    try:
        cases = load_cases(args.cases)

        for model in models:
            probe = OllamaLlmProvider(config.ollama_base_url, model)

            if not await probe.is_available():
                raise EvalError(
                    f"Ollama at {config.ollama_base_url} does not have {model}. "
                    f"Pull it, or drop it from --models: a table with a missing row is worse "
                    f"than no table."
                )

        print(
            f"Running {len(cases)} case(s) through {len(models)} model(s), "
            f"{args.repeat} time(s) each",
            file=sys.stderr,
        )
        outcomes = await run(cases, models, config, args.repeat, args.timeout)
    except EvalError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    print(report(outcomes, cases, models))

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(as_json(outcomes, models), indent=2), encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
