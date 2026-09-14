"""testing-mcp: the checks that answer "is it serving", and the one that writes.

The property worth testing here is the one the tool exists for: **healthy is not serving**. A
service whose connection pool is exhausted passes its own health check and fails every request
that touches the database, and a check that reported only health would call that fixed.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from _shared import approval, config
from testing_mcp import server as testing

SECRET = "dev-only-approval-secret"
WORKFLOW = "testing-mcp/run_checkout_workflow"


def issue(tool: str, arguments: dict[str, Any], *, secret: str = SECRET) -> str:
    import base64
    import hashlib
    import hmac

    claims = {
        "v": approval.SCHEME,
        "rec": "rec-1",
        "tool": tool,
        "args": approval.hash_arguments(arguments),
        "exp": int(time.time()) + 600,
        "by": "operator@sentinel",
    }
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )

    return f"{payload}.{signature}"


class FakeEstate:
    """The five services, answering however a test wants them to."""

    def __init__(self, *, health: int = 200, reads: list[int] | None = None) -> None:
        self.health = health
        self.reads = list(reads or [200] * 5)
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")

        # Checked before the plain health path: /api/services/health ends with "/health" too.
        if path.endswith("/api/services/health"):
            return httpx.Response(
                200,
                json=[
                    {"name": "users", "status": "Healthy"},
                    {"name": "orders", "status": "Unhealthy"},
                ],
            )

        if path.endswith("/health"):
            return httpx.Response(self.health, json={"status": "Healthy"})

        if path.endswith("/api/checkout"):
            payload = json.loads(request.content or b"{}")

            # The services speak snake_case. A payload in any other casing binds to the default
            # and comes back as a 404 about a user nobody named.
            if "user_id" not in payload or "product_name" not in payload["items"][0]:
                return httpx.Response(404, json={"message": "User 00000000-... not found."})

            return httpx.Response(201, json={"id": "order-1", "status": "Created"})

        status = self.reads.pop(0) if self.reads else 200

        return httpx.Response(status, json=[] if status == 200 else {"message": "pool exhausted"})


@pytest.fixture(autouse=True)
def estate(monkeypatch):  # noqa: ANN001, ANN201
    fake = FakeEstate()
    config.settings.cache_clear()
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)

    # The real class, captured before the patch: building one inside the replacement would call
    # the replacement.
    real = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(**{**kwargs, "transport": httpx.MockTransport(fake.handler)})

    monkeypatch.setattr(testing.httpx, "AsyncClient", client)

    yield fake

    config.settings.cache_clear()


async def test_a_serving_service_passes(estate: FakeEstate) -> None:
    check = await testing.run_smoke_check("orders")

    assert check["passed"] is True
    assert check["succeeded"] == testing.SMOKE_REQUESTS
    assert check["latency_ms_median"] is not None
    assert "both pass" in check["note"]


async def test_healthy_and_serving_nothing_is_the_failure_this_tool_is_for(
    estate: FakeEstate,
) -> None:
    """The shape of a pool exhaustion: the process is fine and the work is not."""
    estate.reads = [500] * 5
    check = await testing.run_smoke_check("orders")

    assert check["healthy"] is True
    assert check["passed"] is False
    assert check["success_rate"] == 0.0
    assert "the process is fine and the work is not" in check["note"]
    assert check["failures"][0]["status"] == 500


async def test_partly_serving_is_reported_as_partly_serving(estate: FakeEstate) -> None:
    """A check that only says pass or catastrophe cannot describe a recovery."""
    estate.reads = [200, 500, 200, 500, 200]
    check = await testing.run_smoke_check("orders")

    assert check["success_rate"] == 0.6
    assert check["passed"] is False
    assert "partially serving" in check["note"]


async def test_a_failed_health_check_says_so(estate: FakeEstate) -> None:
    estate.health = 503
    check = await testing.run_smoke_check("orders")

    assert check["healthy"] is False
    assert "not serving" in check["note"]


async def test_an_unknown_service_is_named_with_the_ones_that_exist() -> None:
    check = await testing.run_smoke_check("billing")

    assert check["passed"] is False
    assert "gateway" in check["error"] and "orders" in check["error"]


async def test_the_estate_check_names_what_is_unhealthy() -> None:
    check = await testing.run_estate_check()

    assert check["passed"] is False
    assert check["unhealthy"] == ["orders"]
    assert check["found"] == 2


async def test_the_workflow_check_needs_an_approval(estate: FakeEstate) -> None:
    """It writes an order row, which is the line the policy layer draws."""
    refused = await testing.run_checkout_workflow(user_id="u-1", approval_token="nonsense")

    assert refused["approved"] is False
    assert not any("checkout" in request for request in estate.requests)


async def test_an_approved_workflow_drives_a_real_checkout(estate: FakeEstate) -> None:
    arguments = {"user_id": testing.DEMO_USER}
    outcome = await testing.run_checkout_workflow(
        **arguments, approval_token=issue(WORKFLOW, arguments)
    )

    assert outcome["approved"] is True
    assert outcome["passed"] is True
    assert outcome["order"]["id"] == "order-1"
    assert any("POST /api/checkout" in request for request in estate.requests)
