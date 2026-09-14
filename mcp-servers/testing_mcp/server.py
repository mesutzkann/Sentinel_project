"""testing-mcp: does the system actually serve requests, now that it has been changed?

This is the "test" link of Phase 10's chain — approve, fix, restart, **test**, verify — and it is
not what docs/planning.md §5 imagined. That table lists `run_tests(service)` over `dotnet test`.
Two facts make that the wrong tool to build:

* **The sample services have no test projects.** Tests live in `backend/tests/`; the five
  services under `sample-services/` have none. `dotnet test` against them would run nothing and
  report a pass, which is the worst possible answer to "did the fix work".
* **A green build is not a working system.** The faults this project reproduces — an exhausted
  connection pool, a timeout below the real latency, a leaked handler — are all invisible to a
  unit test and all visible to one real request. After a remediation the question is whether the
  running system serves traffic, not whether a build compiles.

So this server asks the running system. `run_smoke_check` reads health and makes real read
requests; `run_checkout_workflow` drives the user → order → payment → notification flow the whole
estate exists to serve.

**The workflow check is annotated destructive, and that is not pedantry.** It creates an order
row. The policy layer has two classes — read-only runs freely, everything else needs a human's
approval — and a tool that writes to the system's own database belongs in the second however
benign the write is. The smoke check is the one the verification chain calls without asking
anybody, and it is genuinely read-only.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from _shared.config import settings
from _shared.results import result
from _shared.server import build_server, destructive, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "testing-mcp",
    instructions=(
        "Whether the system serves requests right now. Use this after a change to find out "
        "whether it helped: a smoke check reads health and makes real requests through a "
        "service, and the checkout workflow drives a request across all five services the way a "
        "customer would. This is the only server that generates traffic rather than reading what "
        "traffic left behind — everything else here tells you what already happened."
    ),
)

#: Requests one smoke check makes, beyond health. Enough for a failure rate to mean something and
#: few enough that a check is not a load test: a tool that puts a service under pressure while
#: somebody is diagnosing it changes the thing it is measuring.
SMOKE_REQUESTS = 5

#: Per request. A service that is broken is often slow rather than silent, and a check that waits
#: thirty seconds for each of five requests is a check nobody runs during an incident.
REQUEST_TIMEOUT = 10.0

#: What "healthy" needs. Not 100%: one failure in five during a recovery is a real state, and a
#: check that only ever says pass or catastrophe cannot describe it.
PASS_RATE = 0.8

#: Where each service answers on the compose network, and what to ask it for a read check.
SERVICES: dict[str, tuple[str, str]] = {
    "gateway": ("http://gateway:8080", "/api/services/health"),
    "users": ("http://users:8080", "/users"),
    "orders": ("http://orders:8080", "/orders?limit=5"),
    "payments": ("http://payments:8080", "/payments?limit=5"),
    "notifications": ("http://notifications:8080", "/notifications?limit=5"),
}

#: A seeded user, so a workflow check needs no setup. From the users service's own seed data.
DEMO_USER = "33333333-3333-3333-3333-333333333333"


@read_only(
    server,
    "Health plus a handful of real read requests against one service. Use it after a change to "
    "see whether the service is serving, and how fast.",
    idempotent=False,
)
async def run_smoke_check(service: str) -> dict[str, Any]:
    """Health first, then requests, because they fail differently.

    A service can report healthy and still fail every request — a connection pool that is
    exhausted passes a self check and times out on anything that touches the database. So the
    result carries both, and neither is allowed to stand in for the other.
    """
    known = SERVICES.get(service.strip().casefold())

    if known is None:
        return {
            "error": f"'{service}' is not one of {', '.join(sorted(SERVICES))}.",
            "passed": False,
        }

    base, path = known
    health = await _probe(f"{base}/health")
    attempts = [await _probe(f"{base}{path}") for _ in range(SMOKE_REQUESTS)]
    succeeded = [attempt for attempt in attempts if attempt["ok"]]
    rate = len(succeeded) / len(attempts) if attempts else 0.0

    return {
        "service": service,
        # Both conditions, and the health check is not sufficient on its own: see above.
        "passed": health["ok"] and rate >= PASS_RATE,
        "healthy": health["ok"],
        "health_status": health.get("status"),
        "requests": len(attempts),
        "succeeded": len(succeeded),
        "success_rate": round(rate, 2),
        "latency_ms_median": _median([a["latency_ms"] for a in succeeded]),
        "latency_ms_max": max((a["latency_ms"] for a in succeeded), default=None),
        "failures": [
            {"status": a.get("status"), "error": a.get("error")} for a in attempts if not a["ok"]
        ][:SMOKE_REQUESTS],
        "note": _smoke_note(health["ok"], rate),
    }


@read_only(
    server,
    "Health of every service at once, from the gateway's own aggregated check.",
)
async def run_estate_check() -> dict[str, Any]:
    """One request that says which services are up, for the report after a remediation."""
    probe = await _probe(f"{SERVICES['gateway'][0]}/api/services/health")

    if not probe["ok"]:
        return {
            "passed": False,
            "error": probe.get("error") or f"The gateway answered {probe.get('status')}.",
        }

    services = probe.get("body")

    if not isinstance(services, list):
        return {"passed": False, "error": "The gateway's health response was not a list."}

    unhealthy = [
        entry.get("name")
        for entry in services
        if isinstance(entry, dict) and entry.get("status") != "Healthy"
    ]

    return result(
        found=len(services),
        window="now",
        items=[
            {"name": entry.get("name"), "status": entry.get("status")}
            for entry in services
            if isinstance(entry, dict)
        ],
        passed=not unhealthy,
        unhealthy=unhealthy,
    )


@destructive(
    server,
    "Drive one checkout across all five services, the way a customer would. Creates an order, "
    "so it needs an approval.",
)
async def run_checkout_workflow(user_id: str, approval_token: str) -> dict[str, Any]:
    """The end-to-end proof, and the reason it needs approving.

    This is the strongest verification the system has: a checkout touches the gateway, users,
    orders, payments and notifications in one request, so it exercises exactly the path the chaos
    scenarios break. It also writes an order row — synthetic, harmless, and still a write to the
    system's own database, which is the line the policy layer draws.
    """
    started = time.perf_counter()
    probe = await _post(
        f"{SERVICES['gateway'][0]}/api/checkout",
        # snake_case, which is what these services actually accept. Found the hard way: the
        # gateway binds an unknown property to the default and answers "User
        # 00000000-0000-0000-0000-000000000000 not found" — a 404 that names a user nobody asked
        # for, and reads as a missing user rather than as a payload this service could not parse.
        {
            "user_id": user_id or DEMO_USER,
            "items": [{"product_name": "smoke-check", "quantity": 1, "unit_price": 9.99}],
            "currency": "TRY",
        },
    )

    return {
        "passed": probe["ok"],
        "status": probe.get("status"),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "order": probe.get("body") if probe["ok"] else None,
        "error": probe.get("error"),
        "note": (
            "The checkout completed across all five services."
            if probe["ok"]
            else "The checkout did not complete; the response says which hop refused it."
        ),
    }


# -------------------------------------------------------------------------- helpers ----


async def _probe(url: str) -> dict[str, Any]:
    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.get(url)
    except (TimeoutError, httpx.HTTPError) as exc:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "ok": response.is_success,
        "status": response.status_code,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "body": _body(response),
    }


async def _post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(url, json=payload)
    except (TimeoutError, httpx.HTTPError) as exc:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": f"{type(exc).__name__}: {exc}",
        }

    return {
        "ok": response.is_success,
        "status": response.status_code,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "body": _body(response),
        "error": None if response.is_success else _one_line(response.text),
    }


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return _one_line(response.text)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:300]


def _median(values: list[int]) -> int | None:
    if not values:
        return None

    ordered = sorted(values)

    return ordered[len(ordered) // 2]


def _smoke_note(healthy: bool, rate: float) -> str:
    if healthy and rate >= PASS_RATE:
        return "Health and requests both pass."

    if healthy and rate == 0.0:
        # The failure this tool exists to make visible: the shape of a pool exhaustion.
        return (
            "The service reports healthy and answers no requests. That is what a dependency "
            "failure looks like from outside: the process is fine and the work is not."
        )

    if healthy:
        return f"Health passes and {rate:.0%} of requests succeed — partially serving."

    return "The health check itself failed; the service is not serving."


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
