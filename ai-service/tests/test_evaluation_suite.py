"""The suite: one command, and what it records when a benchmark cannot run.

The property worth protecting is that a skip is visible. A suite that quietly omitted the
benchmark whose dependency was missing would produce a report whose gaps read as zeros — and the
gaps are exactly where somebody would look for the number that is not there.
"""

from __future__ import annotations

import json

import pytest

from evaluation.suite import (
    Benchmark,
    rag_headline,
    reasoning_headline,
    render,
    router_headline,
    run_suite,
)


def benchmark(kind: str, writes: dict | None, code: int = 0, raises: Exception | None = None):
    """One fake benchmark, behaving as the real ones do: write JSON, return an exit code."""

    async def run(argv: list[str]) -> int:
        if raises is not None:
            raise raises

        if writes is not None:
            output = argv[argv.index("--output") + 1]

            with open(output, "w", encoding="utf-8") as handle:
                json.dump(writes, handle)

        return code

    return Benchmark(
        kind=kind,
        summary="a benchmark",
        requires="nothing",
        argv=(),
        run=run,
        headline=lambda report: {
            "intent_accuracy": report.get("scores", [{}])[0].get("intent_accuracy")
        },
    )


ROUTER_REPORT = {
    "split": "test",
    "scores": [
        {"router": "rule", "intent_accuracy": 0.354, "examples": 333},
        {
            "router": "sentinel-router [v2]",
            "intent_accuracy": 0.871,
            "service_accuracy": 0.97,
            "tool_f1": 0.731,
            "invalid_json": 0.0,
            "latency_p50": 2051,
            "examples": 333,
        },
    ],
}


@pytest.mark.asyncio
async def test_a_run_records_what_each_benchmark_measured(tmp_path) -> None:
    report = await run_suite([benchmark("router", ROUTER_REPORT)], tmp_path)

    assert len(report.runs) == 1
    assert report.runs[0].ran
    assert report.runs[0].cases == 333
    assert report.runs[0].detail_file == "router.json"

    # The evaluator's own output is kept beside the record: the headline is what a chart draws
    # and the detail is what a person reads when the headline moves.
    written = json.loads((tmp_path / report.run_id / "router.json").read_text(encoding="utf-8"))
    assert written == ROUTER_REPORT


@pytest.mark.asyncio
async def test_a_benchmark_that_cannot_run_is_recorded_as_not_having_run(tmp_path) -> None:
    """The reason travels with the skip. A gap that looks like a zero is worse than no row."""
    report = await run_suite(
        [benchmark("rag", None, raises=RuntimeError("bge-m3 is not loaded"))], tmp_path
    )

    assert not report.runs[0].ran
    assert report.runs[0].status == "failed"
    assert "bge-m3 is not loaded" in report.runs[0].error


@pytest.mark.asyncio
async def test_one_missing_dependency_does_not_abandon_the_others(tmp_path) -> None:
    report = await run_suite(
        [
            benchmark("rag", None, raises=RuntimeError("no database")),
            benchmark("router", ROUTER_REPORT),
        ],
        tmp_path,
    )

    assert [run.status for run in report.runs] == ["failed", "completed"]


@pytest.mark.asyncio
async def test_a_benchmark_that_claims_success_and_writes_nothing_is_a_failure(tmp_path) -> None:
    report = await run_suite([benchmark("router", None)], tmp_path)

    assert not report.runs[0].ran
    assert "wrote no output" in report.runs[0].error


@pytest.mark.asyncio
async def test_the_whole_suite_is_written_as_one_record(tmp_path) -> None:
    report = await run_suite([benchmark("router", ROUTER_REPORT)], tmp_path)
    saved = json.loads((tmp_path / report.run_id / "suite.json").read_text(encoding="utf-8"))

    assert saved["run_id"] == report.run_id
    assert saved["runs"][0]["kind"] == "router"
    assert saved["machine"]


def test_the_router_headline_carries_the_baseline_it_has_to_beat() -> None:
    """A model that beats nothing is not a result, so the keyword table travels with it."""
    headline = router_headline(ROUTER_REPORT)

    assert headline["intent_accuracy"] == 0.871
    assert headline["baseline_router"] == "rule"
    assert headline["baseline_intent_accuracy"] == 0.354


def test_the_rag_headline_takes_the_best_retriever_by_recall() -> None:
    headline = rag_headline(
        {
            "k": 5,
            "scores": [
                {"retriever": "bm25", "recall_at_5": 0.81},
                {"retriever": "hybrid_rerank", "recall_at_5": 0.973, "recall_at_3": 0.956},
            ],
        }
    )

    assert headline["best_retriever"] == "hybrid_rerank"
    assert headline["recall_at_5"] == 0.973


def test_the_reasoning_headline_keeps_every_model() -> None:
    """The comparison is the whole point of that benchmark, so no model is folded away."""
    headline = reasoning_headline(
        {
            "models": {
                "qwen2.5:3b-instruct": {"root_cause_accuracy": 0.6, "completed": 0.4},
                "qwen2.5:7b-instruct": {"root_cause_accuracy": 0.8, "completed": 0.8},
            }
        }
    )

    assert set(headline["models"]) == {"qwen2.5:3b-instruct", "qwen2.5:7b-instruct"}
    assert headline["models"]["qwen2.5:7b-instruct"]["root_cause_accuracy"] == 0.8


@pytest.mark.asyncio
async def test_the_table_says_which_benchmarks_did_not_run(tmp_path) -> None:
    report = await run_suite(
        [benchmark("rag", None, raises=RuntimeError("no database"))], tmp_path
    )
    table = render(report)

    assert "Did not run:" in table
    assert "no database" in table
