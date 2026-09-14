"""The router benchmark: a keyword table, a base model and a tuned one, over the same questions.

    python -m evaluation.router_eval                                  # rules only, instant
    python -m evaluation.router_eval --models qwen2.5:1.5b-instruct   # rules and that model
    python -m evaluation.router_eval --models a,b --unconstrained     # also without the grammar
    python -m evaluation.router_eval --split val --limit 50           # a quick look

**The keyword table is in every run and that is the point of it.** Phase 8's claim is that a
tuned 1.5B beats it; a table with only model rows would be a table with nothing to read. The
questions come from `datasets/routing/test.jsonl`, which was written as questions rather than
from the rule table's vocabulary — see `training/templates.py` for why that distinction decides
whether this benchmark means anything.

What is measured, in the order it matters:

*Intent accuracy.* The number the phase is judged on, reported overall and per intent, because an
average over fifteen classes hides the one that is at zero.

*Service accuracy.* Exact match including null: naming a service the question did not is as wrong
as missing one it did, and it sends the collectors at the wrong service.

*Tool F1.* Micro-averaged over the multi-label tool lists. Expect it to track intent accuracy
closely and say so: the tools follow from the intent through `routing/plans.py`, so this measures
whether a model that got the intent right can also copy a list — which is a real failure mode for
a small model, and not an independent one.

*Invalid JSON.* Zero by construction when the grammar is enforced, which is what ships. Run
`--unconstrained` for the number that is about the model.

*Latency.* p50 and p95 per question. The planning document's target is 150 ms, and a router that
is accurate and slow moves the agent's cost onto every investigation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from llm.ollama_provider import OllamaLlmProvider
from routing.base import Router
from routing.model_router import ModelRouter
from routing.rule_router import RuleBasedRouter
from routing.schema import Intent
from training.routing_dataset import DEFAULT_OUTPUT, Example, load


@dataclass
class Outcome:
    """What one router did with one question."""

    example_id: str
    query: str
    language: str
    source: str
    expected_intent: str
    predicted_intent: str | None
    expected_service: str | None
    predicted_service: str | None
    expected_tools: list[str]
    predicted_tools: list[str]
    latency_ms: int
    invalid: bool = False

    @property
    def intent_correct(self) -> bool:
        return self.predicted_intent == self.expected_intent

    @property
    def service_correct(self) -> bool:
        return self.predicted_service == self.expected_service


@dataclass
class Scores:
    """One router's row in the table."""

    router: str
    examples: int
    intent_accuracy: float
    service_accuracy: float
    tool_precision: float
    tool_recall: float
    tool_f1: float
    invalid_json: float
    latency_p50: int
    latency_p95: int
    per_intent: dict[str, float] = field(default_factory=dict)
    per_language: dict[str, float] = field(default_factory=dict)
    confusions: list[tuple[str, str, int]] = field(default_factory=list)


async def run_router(router: Router, examples: list[Example], raw: bool) -> list[Outcome]:
    """Ask one router every question, one at a time.

    Sequential on purpose: latency is one of the numbers, and a router measured under a dozen
    concurrent requests would report the throughput of the batch rather than what the agent waits
    for a single question.
    """
    outcomes: list[Outcome] = []

    for position, example in enumerate(examples, start=1):
        started = time.perf_counter()
        invalid = False

        try:
            # `raw` reads what the model actually emitted. `route` would repair the tools from
            # the plan table, which would score the table rather than the model.
            if raw and isinstance(router, ModelRouter):
                decision = await router.raw(example.query)
            else:
                decision = await router.route(example.query)

            predicted_intent = decision.intent.value
            predicted_service = decision.target_service
            predicted_tools = list(decision.tools)
        except Exception:  # noqa: BLE001 - an unusable answer is a result, not a crash
            invalid = True
            predicted_intent = None
            predicted_service = None
            predicted_tools = []

        outcomes.append(
            Outcome(
                example_id=example.id,
                query=example.query,
                language=example.language,
                source=example.source,
                expected_intent=example.intent,
                predicted_intent=predicted_intent,
                expected_service=example.target_service,
                predicted_service=predicted_service,
                expected_tools=list(example.tools),
                predicted_tools=predicted_tools,
                latency_ms=int((time.perf_counter() - started) * 1000),
                invalid=invalid,
            )
        )

        if position % 50 == 0:
            print(f"  {router.name}: {position}/{len(examples)}", file=sys.stderr)

    return outcomes


def score(router: str, outcomes: list[Outcome]) -> Scores:
    total = len(outcomes)
    latencies = sorted(o.latency_ms for o in outcomes)

    # Micro-averaged: every tool of every question counts once, so an intent that carries five
    # tools weighs five times an intent that carries one. That is the right weighting here —
    # what is being measured is how many tool names the agent would be handed correctly.
    matched = predicted = expected = 0

    for outcome in outcomes:
        got = set(outcome.predicted_tools)
        want = set(outcome.expected_tools)
        matched += len(got & want)
        predicted += len(got)
        expected += len(want)

    precision = matched / predicted if predicted else 0.0
    recall = matched / expected if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    per_intent: dict[str, float] = {}

    for intent in Intent:
        rows = [o for o in outcomes if o.expected_intent == intent.value]

        if rows:
            per_intent[intent.value] = sum(o.intent_correct for o in rows) / len(rows)

    per_language: dict[str, float] = {}

    for language in sorted({o.language for o in outcomes}):
        rows = [o for o in outcomes if o.language == language]
        per_language[language] = sum(o.intent_correct for o in rows) / len(rows)

    confusions: dict[tuple[str, str], int] = {}

    for outcome in outcomes:
        if not outcome.intent_correct and outcome.predicted_intent:
            key = (outcome.expected_intent, outcome.predicted_intent)
            confusions[key] = confusions.get(key, 0) + 1

    return Scores(
        router=router,
        examples=total,
        intent_accuracy=sum(o.intent_correct for o in outcomes) / total if total else 0.0,
        service_accuracy=sum(o.service_correct for o in outcomes) / total if total else 0.0,
        tool_precision=precision,
        tool_recall=recall,
        tool_f1=f1,
        invalid_json=sum(o.invalid for o in outcomes) / total if total else 0.0,
        latency_p50=int(statistics.median(latencies)) if latencies else 0,
        latency_p95=latencies[int(len(latencies) * 0.95)] if latencies else 0,
        per_intent=per_intent,
        per_language=per_language,
        confusions=sorted(
            ((a, b, n) for (a, b), n in confusions.items()), key=lambda row: row[2], reverse=True
        )[:8],
    )


def report(scores: list[Scores]) -> str:
    lines = [
        "",
        "## Routers",
        "",
        "| router | intent | service | tool P | tool R | tool F1 | invalid JSON | p50 | p95 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for row in scores:
        lines.append(
            f"| {row.router} | {row.intent_accuracy:.3f} | {row.service_accuracy:.3f} | "
            f"{row.tool_precision:.3f} | {row.tool_recall:.3f} | {row.tool_f1:.3f} | "
            f"{row.invalid_json:.1%} | {row.latency_p50} ms | {row.latency_p95} ms |"
        )

    lines += ["", "## Intent accuracy, per intent", ""]
    lines.append("| intent | " + " | ".join(row.router for row in scores) + " |")
    lines.append("|---" * (len(scores) + 1) + "|")

    for intent in sorted(scores[0].per_intent):
        cells = " | ".join(f"{row.per_intent.get(intent, 0):.2f}" for row in scores)
        lines.append(f"| {intent} | {cells} |")

    lines += ["", "## By language", ""]
    lines.append("| language | " + " | ".join(row.router for row in scores) + " |")
    lines.append("|---" * (len(scores) + 1) + "|")

    for language in sorted(scores[0].per_language):
        cells = " | ".join(f"{row.per_language.get(language, 0):.2f}" for row in scores)
        lines.append(f"| {language} | {cells} |")

    for row in scores:
        if not row.confusions:
            continue

        lines += ["", f"### What {row.router} confused", ""]

        for expected, predicted, count in row.confusions:
            lines.append(f"- {expected} -> {predicted} ({count})")

    return "\n".join(lines)


def as_json(
    scores: list[Scores],
    outcomes: dict[str, list[Outcome]],
    *,
    split: str,
    prompt: str,
) -> dict[str, Any]:
    """The run, with enough about itself to be read months later.

    The metadata is not decoration. `/models` in the AI service publishes this file to the
    frontend, and a table of accuracies with no split, no prompt version and no date is a table
    nobody can check — the same numbers mean different things on `val` and on `test`, and the
    split itself has changed once already this phase.
    """
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "split": split,
        "prompt": prompt,
        "examples": scores[0].examples if scores else 0,
        "scores": [row.__dict__ for row in scores],
        "outcomes": {
            name: [outcome.__dict__ for outcome in rows] for name, rows in outcomes.items()
        },
    }


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--split", default="test", choices=("train", "val", "test"))
    parser.add_argument("--models", default="", help="comma-separated Ollama model names")
    parser.add_argument(
        "--prompt",
        default="v1",
        help=(
            "which router prompt the models in this run are given. v1 carries the fifteen "
            "intents and their tools, which a base model has to be told; v2 is one line, which "
            "is what a tuned model is trained on and all it needs"
        ),
    )
    parser.add_argument(
        "--unconstrained",
        action="store_true",
        help=(
            "also measure each model without the schema grammar, which is the honest "
            "invalid-JSON number"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="sample N questions instead of the whole split",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    examples = load(args.dataset, args.split)

    if not examples:
        print(
            f"No {args.split} split in {args.dataset}. Build it with "
            f"`python -m training.routing_dataset`.",
            file=sys.stderr,
        )

        return 1

    if args.limit and args.limit < len(examples):
        # Sampled, not the first N. The file is written split-by-split and grouped by intent, so
        # a prefix is a handful of intents rather than a small version of the benchmark — the
        # first sixty rows of the test split gave the keyword table 0.60 where the whole split
        # gives it 0.45.
        examples = random.Random(1).sample(examples, args.limit)

    config = settings()
    routers: list[tuple[Router, bool]] = [(RuleBasedRouter(), False)]

    for model in [name.strip() for name in args.models.split(",") if name.strip()]:
        provider = OllamaLlmProvider(config.ollama_base_url, model, timeout_seconds=120)

        if not await provider.is_available():
            print(f"Ollama does not have {model}. Pull it or drop it.", file=sys.stderr)

            return 1

        label = f"{model} [{args.prompt}]"
        routers.append((ModelRouter(provider, name=label, prompt_version=args.prompt), True))

        if args.unconstrained:
            routers.append(
                (
                    ModelRouter(
                        provider,
                        name=f"{label} no grammar",
                        prompt_version=args.prompt,
                        constrained=False,
                    ),
                    True,
                )
            )

    print(f"{len(examples)} questions from {args.split}, {len(routers)} routers", file=sys.stderr)

    scores: list[Scores] = []
    outcomes: dict[str, list[Outcome]] = {}

    for router, raw in routers:
        rows = await run_router(router, examples, raw)
        outcomes[router.name] = rows
        scores.append(score(router.name, rows))

    print(report(scores))

    if args.output:
        args.output.write_text(
            json.dumps(
                as_json(scores, outcomes, split=args.split, prompt=args.prompt),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8"
        )
        print(f"\nWrote {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
