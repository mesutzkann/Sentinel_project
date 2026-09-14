"""The ``/evaluations`` endpoints: what has been measured on this checkout, and when.

The Evaluation page reads these. They publish what `python -m evaluation.suite` wrote and nothing
else — there is no "run" endpoint here, and that is deliberate twice over:

* **A benchmark run is minutes of GPU, not an HTTP request.** The router benchmark is seven
  minutes, RAG is five, the reasoning comparison longer; an endpoint that started one would be an
  endpoint whose only honest answer is 202 plus a polling contract for something a person runs
  from a terminal once a week.
* **A page that can start a run can start one by accident.** Two people opening the same screen
  during an incident would put two benchmarks on the GPU the investigation needs.

So the suite is a command, and this is the window onto what it left behind. A checkout where
nobody has run it says so rather than drawing an empty chart.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings, settings
from evaluation.suite import DEFAULT_RUNS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

#: How many runs a listing returns. A suite run is a directory; a checkout that has been
#: benchmarked weekly for a year would otherwise hand the page three hundred of them.
DEFAULT_LIMIT = 20


# ---------------------------------------------------------------------- contracts ----


class BenchmarkRunDto(BaseModel):
    kind: str
    status: str
    started_at: str
    duration_ms: int = 0
    cases: int = 0
    metrics: dict[str, Any] = Field(default_factory=dict)
    detail_file: str | None = None
    error: str | None = None


class SuiteRunDto(BaseModel):
    run_id: str
    started_at: str
    duration_ms: int
    machine: str
    runs: list[BenchmarkRunDto] = Field(default_factory=list)


class EvaluationsResponse(BaseModel):
    total: int
    runs: list[SuiteRunDto] = Field(default_factory=list)

    #: What to type when there is nothing here. A page that says "no data" and stops is a page
    #: that makes somebody go and read the source to find out how data gets there.
    command: str = "python -m evaluation.suite"


# ------------------------------------------------------------------ dependencies ----


def get_settings() -> Settings:
    return settings()


def runs_dir(config: Settings) -> Path:  # noqa: ARG001 - the path is a convention, not a setting
    """Where the suite writes. A convention rather than a setting, like the router benchmark's
    report: what the page claims must not depend on how the process was started."""
    return DEFAULT_RUNS_DIR


# ---------------------------------------------------------------------- endpoints ----


@router.get("", response_model=EvaluationsResponse)
async def list_runs(
    config: Annotated[Settings, Depends(get_settings)],
    limit: int = DEFAULT_LIMIT,
) -> EvaluationsResponse:
    """Every suite run on this checkout, newest first."""
    found = read_runs(runs_dir(config), limit=max(1, min(limit, 100)))

    return EvaluationsResponse(total=len(found), runs=found)


@router.get("/{run_id}", response_model=SuiteRunDto)
async def read_run(
    run_id: str,
    config: Annotated[Settings, Depends(get_settings)],
) -> SuiteRunDto:
    found = read_one(runs_dir(config), run_id)

    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No evaluation run {run_id} on this checkout.",
        )

    return found


@router.get("/{run_id}/{kind}", response_model=dict)
async def read_detail(
    run_id: str,
    kind: str,
    config: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """One benchmark's full output: every query, every question, every answer.

    The headline is what a chart draws; this is what somebody reads when the headline moves. A
    mean that dropped is a question and the case that stopped working is the answer, and it is
    not recoverable from the mean.
    """
    # Resolved and checked rather than concatenated: `kind` reaches this from a URL, and
    # `../../etc/passwd` is a file name too.
    directory = (runs_dir(config) / run_id).resolve()
    path = (directory / f"{kind}.json").resolve()

    if not path.is_file() or runs_dir(config).resolve() not in path.parents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {kind} detail in run {run_id}.",
        )

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"The {kind} detail of run {run_id} could not be read: {exc}",
        ) from exc


# ------------------------------------------------------------------------ helpers ----


def read_runs(directory: Path, limit: int = DEFAULT_LIMIT) -> list[SuiteRunDto]:
    """The runs on disk, newest first. A directory that is not a run is skipped, not fatal."""
    if not directory.is_dir():
        return []

    runs: list[SuiteRunDto] = []

    # The run id is a UTC timestamp, so lexical order is chronological and no file has to be
    # opened to sort them.
    for child in sorted(directory.iterdir(), key=lambda path: path.name, reverse=True):
        if len(runs) >= limit:
            break

        parsed = _read(child / "suite.json")

        if parsed is not None:
            runs.append(parsed)

    return runs


def read_one(directory: Path, run_id: str) -> SuiteRunDto | None:
    candidate = (directory / run_id).resolve()

    if directory.resolve() not in candidate.parents:
        return None

    return _read(candidate / "suite.json")


def _read(path: Path) -> SuiteRunDto | None:
    if not path.is_file():
        return None

    try:
        return SuiteRunDto(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        logger.warning("%s is not a readable suite run", path)

        return None
