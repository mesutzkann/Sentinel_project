"""The `/evaluations` surface: what has been measured, and what it refuses to do.

It publishes what `python -m evaluation.suite` wrote and nothing else. There is no run endpoint
and these tests assert its absence, because a page that can start a benchmark can start two by
accident during an incident — on the card the investigation is using.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import evaluations as evaluations_api
from app.main import app

RUN = {
    "run_id": "20260914T145257Z",
    "started_at": "2026-09-14T14:52:57+00:00",
    "duration_ms": 512000,
    "machine": "Windows AMD64",
    "runs": [
        {
            "kind": "router",
            "status": "completed",
            "started_at": "2026-09-14T14:52:57+00:00",
            "duration_ms": 410000,
            "cases": 333,
            "metrics": {"intent_accuracy": 0.871, "baseline_intent_accuracy": 0.354},
            "detail_file": "router.json",
            "error": None,
        },
        {
            "kind": "rag",
            "status": "failed",
            "started_at": "2026-09-14T14:59:00+00:00",
            "duration_ms": 900,
            "cases": 0,
            "metrics": {},
            "detail_file": None,
            "error": "EmbeddingUnavailableError: bge-m3 is not loaded",
        },
    ],
}


@pytest.fixture
def runs(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """Two runs on disk, the newer one second so ordering is actually exercised."""
    for run_id in ("20260901T090000Z", "20260914T145257Z"):
        directory = tmp_path / run_id
        directory.mkdir()
        (directory / "suite.json").write_text(
            json.dumps({**RUN, "run_id": run_id}), encoding="utf-8"
        )
        (directory / "router.json").write_text(
            json.dumps({"scores": [{"router": "rule", "intent_accuracy": 0.354}]}),
            encoding="utf-8",
        )

    monkeypatch.setattr(evaluations_api, "DEFAULT_RUNS_DIR", tmp_path)
    monkeypatch.setattr(evaluations_api, "runs_dir", lambda config: tmp_path)  # noqa: ARG005

    with TestClient(app) as client:
        yield client


def test_runs_come_back_newest_first(runs: TestClient) -> None:
    body = runs.get("/evaluations").json()

    assert body["total"] == 2
    assert [run["run_id"] for run in body["runs"]] == ["20260914T145257Z", "20260901T090000Z"]


def test_a_benchmark_that_did_not_run_keeps_its_reason(runs: TestClient) -> None:
    body = runs.get("/evaluations").json()
    rag = next(run for run in body["runs"][0]["runs"] if run["kind"] == "rag")

    assert rag["status"] == "failed"
    assert "bge-m3" in rag["error"]


def test_the_full_detail_of_one_benchmark_is_available(runs: TestClient) -> None:
    body = runs.get("/evaluations/20260914T145257Z/router").json()

    assert body["scores"][0]["router"] == "rule"


def test_a_detail_path_cannot_escape_the_runs_directory(runs: TestClient) -> None:
    """`kind` arrives from a URL, and `../../etc/passwd` is a file name too."""
    for attempt in ("../suite", "..%2Fsuite", "....//suite"):
        assert runs.get(f"/evaluations/20260914T145257Z/{attempt}").status_code in (404, 422)


def test_an_unknown_run_is_a_404(runs: TestClient) -> None:
    assert runs.get("/evaluations/20250101T000000Z").status_code == 404


def test_an_empty_checkout_says_what_to_run(tmp_path, monkeypatch) -> None:
    """"No data" and nothing else makes somebody read the source to find out how data arrives."""
    monkeypatch.setattr(evaluations_api, "runs_dir", lambda config: tmp_path / "nothing")  # noqa: ARG005

    with TestClient(app) as client:
        body = client.get("/evaluations").json()

    assert body["total"] == 0
    assert body["command"] == "python -m evaluation.suite"


def test_there_is_no_way_to_start_a_run(runs: TestClient) -> None:
    """A benchmark is minutes of GPU, and this page is read during incidents."""
    assert runs.post("/evaluations/run").status_code in (404, 405)
    assert runs.post("/evaluations").status_code in (404, 405)
