"""The demo: break something for real, then watch the agent work out what happened.

    python scripts/demo.py                 # the whole thing, ~4 minutes
    python scripts/demo.py --keep          # leave the scenario enabled afterwards
    python scripts/demo.py --skip-load     # telemetry is already there; just investigate

It runs the chain the project is built around and nothing else:

    reset  ->  healthy load  ->  enable scenario 1  ->  load  ->  incident  ->  investigate

**Nothing here tells the agent anything.** The incident it is handed says what a person would
say — "orders is timing out" — and the scenario code appears nowhere in the question, the service
hint or the evidence. The only way the run reaches `DB_CONNECTION_POOL_EXHAUSTION` is by
collecting the telemetry that enabling it produced. That is the whole claim of the project, and
a demo that leaked the answer would be a demo of nothing.

One Python script rather than a PowerShell one and a bash one, because this is the piece most
likely to be read by somebody deciding whether the project works, and two copies of it would
drift. It starts nothing: if a service is down it says which and what to run, because owning the
lifecycle of four processes is what `scripts/dev.ps1` and the Makefile are for.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]

SCENARIO = "DB_CONNECTION_POOL_EXHAUSTION"
SCENARIO_SERVICE = "orders"

# What a person would say at three in the morning. No codes, no hints.
INCIDENT_TITLE = "Orders is timing out"
INCIDENT_DESCRIPTION = "Checkout requests hang and then fail. Started a few minutes ago."
QUESTION = "orders is timing out, find out why"

# Long enough for Loki, Prometheus and Jaeger to have a healthy period to contrast against, and
# short enough that nobody walks away from the terminal.
BASELINE_SECONDS = 12
CHAOS_SECONDS = 75

# Measured, and the number the demo lives or dies on.
#
# The scenario leaves 20 connections, holds each for 800 ms, and gives up waiting after 5 s — so
# the pool is only *exhausted*, rather than merely busy, once the queue is long enough that a
# request waits out those five seconds. Driving orders at 60, 120 and 200 concurrent for twenty
# seconds each: 0% failed, 37% failed, 35% failed. Below the knee the requests queue politely and
# succeed, the telemetry shows a slow but healthy service, and the agent correctly investigates an
# incident that is not happening. 150 sits above it with margin.
CONCURRENCY = 150

# The OTel collector batches, and the agent asks the observability stack rather than the service.
# Investigating before the batch lands finds a healthy system and concludes so, correctly.
SETTLE_SECONDS = 20

POLL_SECONDS = 3


class DemoError(RuntimeError):
    """Something the operator has to fix before the demo can run."""


def env(name: str, fallback: str) -> str:
    """Read one value out of the repository `.env`, which every component already shares."""
    path = ROOT / ".env"

    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()

            if stripped.startswith(f"{name}=") and not stripped.startswith("#"):
                return stripped.split("=", 1)[1].strip()

    return fallback


BACKEND = f"http://localhost:{env('BACKEND_PORT', '5080')}"
AI_SERVICE = f"http://localhost:{env('AI_SERVICE_PORT', '8000')}"
ORDERS = f"http://localhost:{env('ORDERS_PORT', '8082')}"
FRONTEND = "http://localhost:5173"
ADMIN = (env("SEED_ADMIN_USERNAME", "admin"), env("SEED_ADMIN_PASSWORD", "Admin123!"))


# ---------------------------------------------------------------------- output ----

BOLD, DIM, GREEN, AMBER, RED, RESET = (
    "\033[1m",
    "\033[2m",
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[0m",
)


def step(number: int, text: str) -> None:
    print(f"\n{BOLD}[{number}/6] {text}{RESET}", flush=True)


def note(text: str) -> None:
    print(f"      {DIM}{text}{RESET}", flush=True)


def good(text: str) -> None:
    print(f"      {GREEN}{text}{RESET}", flush=True)


def warn(text: str) -> None:
    print(f"      {AMBER}{text}{RESET}", flush=True)


# ---------------------------------------------------------------------- preflight ----


async def preflight(client: httpx.AsyncClient) -> None:
    """Everything the chain needs, named individually so a failure says what to start."""
    checks = [
        ("the backend", f"{BACKEND}/health", "./scripts/dev.ps1 backend  (or: make backend)"),
        (
            "the AI service",
            f"{AI_SERVICE}/health",
            "cd ai-service && .venv/Scripts/uvicorn app.main:app --port 8000",
        ),
        (
            "the orders sample",
            f"{ORDERS}/health/ready",
            "docker compose --profile core --profile samples up -d",
        ),
    ]

    missing: list[str] = []

    for name, url, fix in checks:
        try:
            response = await client.get(url, timeout=5)
            response.raise_for_status()
            good(f"{name} is up")
        except Exception:
            missing.append(f"{name} is not answering at {url}\n        start it with: {fix}")

    # The agent can reason without MCP servers and will conclude nothing useful, so this is worth
    # failing on rather than discovering in the timeline.
    try:
        tools = (await client.get(f"{AI_SERVICE}/mcp/tools", timeout=30)).json()
        reachable = [s for s in tools["servers"] if s["reachable"]]
        good(f"{len(reachable)} of {len(tools['servers'])} MCP servers reachable, "
             f"{tools['total_tools']} tools")

        if len(reachable) < 4:
            missing.append(
                "fewer than four MCP servers are reachable; the agent would investigate blind\n"
                "        start them with: docker compose --profile mcp up -d"
            )
    except Exception as exc:
        missing.append(f"the MCP registry could not be read: {exc}")

    if missing:
        raise DemoError("\n      ".join(missing))


# ---------------------------------------------------------------------- the estate ----


async def chaos(client: httpx.AsyncClient, path: str) -> None:
    response = await client.post(f"{ORDERS}/chaos/{path}", timeout=60)
    response.raise_for_status()


async def drive(client: httpx.AsyncClient, seconds: int, label: str) -> tuple[int, int]:
    """Hold `CONCURRENCY` requests against orders for `seconds`, counting what happened.

    `GET /orders` is the endpoint the scenario makes hold a connection, so this is the load that
    actually exhausts the pool rather than merely warming the service.
    """
    deadline = time.monotonic() + seconds
    ok = 0
    failed = 0
    lock = asyncio.Lock()

    async def worker() -> None:
        nonlocal ok, failed

        while time.monotonic() < deadline:
            try:
                response = await client.get(f"{ORDERS}/orders?limit=50", timeout=30)
                success = response.status_code < 500
            except Exception:
                success = False

            async with lock:
                if success:
                    ok += 1
                else:
                    failed += 1

    reporter = asyncio.create_task(progress(label, seconds))

    try:
        await asyncio.gather(*(worker() for _ in range(CONCURRENCY)))
    finally:
        reporter.cancel()

    print(flush=True)

    return ok, failed


async def progress(label: str, seconds: int) -> None:
    """A ticking line, because a silent minute reads as a hang.

    Only on a terminal: carriage returns in a captured log produce one enormous line, and the
    demo's output is the sort of thing that gets pasted into an issue.
    """
    if not sys.stdout.isatty():
        print(f"      {DIM}{label}, {seconds}s{RESET}", flush=True)
        return

    started = time.monotonic()

    try:
        while True:
            elapsed = int(time.monotonic() - started)
            print(f"\r      {DIM}{label} {elapsed}s / {seconds}s{RESET}", end="", flush=True)
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        return


# ---------------------------------------------------------------------- the platform ----


async def sign_in(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post(
        f"{BACKEND}/api/auth/login",
        json={"username": ADMIN[0], "password": ADMIN[1]},
        timeout=30,
    )

    if response.status_code != 200:
        raise DemoError(
            f"could not sign in as {ADMIN[0]}: {response.status_code} {response.text[:200]}"
        )

    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def raise_incident(client: httpx.AsyncClient, headers: dict[str, str]) -> dict:
    services = (await client.get(f"{BACKEND}/api/services", headers=headers, timeout=30)).json()
    service = next((s for s in services if s["name"] == SCENARIO_SERVICE), None)

    if service is None:
        raise DemoError(f"the backend has no service called {SCENARIO_SERVICE}")

    response = await client.post(
        f"{BACKEND}/api/incidents",
        headers=headers,
        json={
            "service_id": service["id"],
            "title": INCIDENT_TITLE,
            "description": INCIDENT_DESCRIPTION,
            "severity": "high",
        },
        timeout=30,
    )

    if response.status_code != 201:
        raise DemoError(f"could not raise the incident: {response.status_code} {response.text[:200]}")

    return response.json()


async def investigate(client: httpx.AsyncClient, headers: dict[str, str], incident_id: str) -> dict:
    response = await client.post(
        f"{BACKEND}/api/incidents/{incident_id}/investigate",
        headers=headers,
        json={"query": QUESTION, "service_hint": SCENARIO_SERVICE},
        timeout=60,
    )

    if response.status_code != 202:
        raise DemoError(
            f"the backend would not start an investigation: "
            f"{response.status_code} {response.text[:300]}"
        )

    return response.json()


async def follow(client: httpx.AsyncClient, headers: dict[str, str], investigation_id: str) -> dict:
    """Print each step as it lands, and return the finished investigation.

    Polling rather than SignalR: this is a terminal, the hub is the browser's channel, and a
    demo that needed a WebSocket client to show its own progress would be demonstrating the
    wrong thing.
    """
    seen: set[int] = set()
    started = time.monotonic()

    while True:
        detail = (
            await client.get(
                f"{BACKEND}/api/investigations/{investigation_id}", headers=headers, timeout=30
            )
        ).json()

        for step_row in sorted(detail["steps"], key=lambda s: s["sequence"]):
            if step_row["sequence"] in seen:
                continue

            seen.add(step_row["sequence"])
            elapsed = int(time.monotonic() - started)
            print(
                f"      {DIM}{elapsed:>4}s{RESET}  {step_row['state']:<28} {step_row['message'][:96]}",
                flush=True,
            )

        status = detail["investigation"]["status"]

        if status not in {"running", "queued"}:
            return detail

        if time.monotonic() - started > 900:
            raise DemoError("the investigation has been running for fifteen minutes; giving up")

        await asyncio.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------- the verdict ----


def report(detail: dict) -> bool:
    """Print what the agent concluded, and say plainly whether it was right."""
    investigation = detail["investigation"]
    root_cause = detail.get("root_cause")

    print(f"\n{BOLD}The verdict{RESET}")

    sources = sorted({item["source"] for item in detail["evidence"]})
    print(
        f"      {len(detail['evidence'])} facts over {len(sources)} sources "
        f"({', '.join(sources)}), {investigation['tool_calls']} tool calls, "
        f"{investigation['llm_calls']} model calls, "
        f"{(investigation.get('total_duration_ms') or 0) / 1000:.0f}s"
    )

    if root_cause is None:
        warn("no root cause: the run ended without a conclusion")
        print(f"      {investigation.get('failure_reason') or ''}")

        return False

    correct = root_cause["category"] == SCENARIO
    mark = f"{GREEN}correct{RESET}" if correct else f"{RED}wrong{RESET}"

    print(f"\n      {BOLD}{root_cause['title']}{RESET}")
    print(f"      {root_cause['category']}  ({mark}; the scenario enabled was {SCENARIO})")
    print(f"      confidence {root_cause['confidence']:.2f}")

    if root_cause.get("explanation"):
        print(f"\n      {root_cause['explanation'][:600]}")

    if detail["recommendations"]:
        print(f"\n      {BOLD}Recommended, pending approval{RESET}")

        for recommendation in detail["recommendations"]:
            print(f"      · {recommendation['action_code']}: {recommendation['description'][:120]}")
    else:
        warn("\n      nothing recommended")
        print(f"      {investigation.get('failure_reason') or ''}")

    return correct


# ---------------------------------------------------------------------- the demo ----


async def run(args: argparse.Namespace) -> int:
    # httpx allows 100 connections by default, which would cap the load below the knee measured
    # above and make the scenario look like it does not reproduce.
    limits = httpx.Limits(max_connections=CONCURRENCY * 2, max_keepalive_connections=CONCURRENCY * 2)

    async with httpx.AsyncClient(follow_redirects=True, limits=limits) as client:
        step(1, "Checking the stack")
        await preflight(client)

        headers = await sign_in(client)
        good(f"signed in as {ADMIN[0]}")

        if not args.skip_load:
            step(2, "Clearing any scenario and driving healthy traffic")
            await chaos(client, "reset")
            ok, failed = await drive(client, BASELINE_SECONDS, "healthy load")
            good(f"{ok} requests served, {failed} failed — this is the 'before' the agent compares against")

            step(3, f"Enabling {SCENARIO} on {SCENARIO_SERVICE}")
            await chaos(client, f"{SCENARIO}/enable")
            note("MaxPoolSize drops from 200 to 20; nothing else changes")

            ok, failed = await drive(client, CHAOS_SECONDS, "load against the broken service")
            share = 100 * failed / max(ok + failed, 1)
            good(f"{ok} served, {failed} failed ({share:.0f}%) — timeouts waiting for a connection")

            if failed == 0:
                warn(
                    "nothing failed, so the pool is busy rather than exhausted and the telemetry "
                    "shows a slow but healthy service. Raise CONCURRENCY."
                )

            note(f"waiting {SETTLE_SECONDS}s for the collector to flush to Loki, Prometheus and Jaeger")
            await asyncio.sleep(SETTLE_SECONDS)
        else:
            step(2, "Skipping the load; using whatever telemetry is already there")
            step(3, "Skipping the scenario; leaving it as it is")

        step(4, "Raising an incident, in the words a person would use")
        incident = await raise_incident(client, headers)
        good(f"{incident['incident_code']}: {incident['title']}")
        note(f"the question: \"{QUESTION}\" — the scenario code appears nowhere")

        step(5, "Investigating")
        investigation = await investigate(client, headers, incident["id"])
        note(f"watch it at {FRONTEND}/investigations/{investigation['id']}")
        print()

        detail = await follow(client, headers, investigation["id"])

        step(6, "Done")
        correct = report(detail)

        if not args.keep:
            await chaos(client, "reset")
            note("\n      scenario reset; the estate is healthy again")
        else:
            warn(f"\n      {SCENARIO} is still enabled. Reset it with: "
                 f"curl -X POST {ORDERS}/chaos/reset")

        if detail["investigation"]["status"] == "failed":
            return 1

        # A wrong category is a result, not a broken demo: the agent is a 3B model reading real
        # telemetry, and the honest thing is to print what it concluded and leave the judgement
        # on the screen. Only a run that could not happen at all is a failure here.
        return 0 if correct or detail.get("root_cause") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="leave the scenario enabled at the end")
    parser.add_argument(
        "--skip-load",
        action="store_true",
        help="do not touch the scenario or generate traffic; investigate what is already there",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    except DemoError as exc:
        print(f"\n{RED}Cannot run the demo:{RESET}\n      {exc}", file=sys.stderr)

        return 2
    except KeyboardInterrupt:
        print(f"\n{AMBER}Stopped. The scenario may still be enabled: "
              f"curl -X POST {ORDERS}/chaos/reset{RESET}", file=sys.stderr)

        return 130


if __name__ == "__main__":
    raise SystemExit(main())
