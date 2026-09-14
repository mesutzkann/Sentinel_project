"""docker-mcp, and the two boundaries that make a daemon socket safe to mount.

A container with `/var/run/docker.sock` can do anything to the host's Docker. Two things stop
that being what this server offers: an approval that names the exact call, checked here and not
only by the caller, and a name prefix that keeps every tool inside this project's stack.

The Docker API is faked at the transport. That is deliberate — these tests are about the two
boundaries and about the shapes the daemon's JSON is turned into, and a test that needed a real
daemon would be a test nobody runs on a laptop.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from _shared import approval, config
from docker_mcp import server as docker

SECRET = "dev-only-approval-secret"
RESTART = "docker-mcp/restart_container"
UPDATE = "docker-mcp/update_env_and_restart"


def issue(tool: str, arguments: dict[str, Any], *, secret: str = SECRET, ttl: int = 600) -> str:
    """The backend's half of the scheme, written out so these tests can approve something.

    Kept here rather than imported from the AI service: these are separate packages in separate
    containers, and a test that could only run with both importable would not run in CI for
    either.
    """
    import base64
    import hashlib
    import hmac

    claims = {
        "v": approval.SCHEME,
        "rec": "rec-1",
        "tool": tool,
        "args": approval.hash_arguments(arguments),
        "exp": int(time.time()) + ttl,
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


CONTAINER = {
    "Id": "abc123",
    "Names": ["/sentinel-orders"],
    "Image": "sentinel/orders",
    "State": "running",
    "Status": "Up 3 hours (healthy)",
    "Created": 1757000000,
}

DETAIL = {
    "Id": "abc123",
    "Name": "/sentinel-orders",
    "RestartCount": 11,
    "State": {
        "Status": "running",
        "Running": True,
        "ExitCode": 0,
        "StartedAt": "2026-09-14T09:00:00Z",
        "FinishedAt": "2026-09-14T08:59:00Z",
        "Health": {"Status": "healthy"},
    },
    "Config": {
        "Image": "sentinel/orders",
        "Env": ["ConnectionStrings__Default=Host=postgres;MaxPoolSize=20", "ASPNETCORE_ENV=Dev"],
        "Cmd": ["dotnet", "Orders.dll"],
    },
    "HostConfig": {"NetworkMode": "sentinel"},
    "NetworkSettings": {"Networks": {"sentinel": {"Aliases": ["orders"]}}},
}


class FakeDaemon:
    """Answers the five endpoints this server uses, and records what it was asked."""

    def __init__(self, **overrides: Any) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self._overrides = overrides
        self.detail = json.loads(json.dumps(DETAIL))

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path, dict(request.url.params)))

        for suffix, response in self._overrides.items():
            if path.endswith(suffix.replace("_", "/")):
                return response

        if path.endswith("/containers/json"):
            return httpx.Response(200, json=[CONTAINER, {"Names": ["/other-thing"]}])

        if path.endswith("/json") and "/containers/" in path:
            return httpx.Response(200, json=self.detail)

        if path.endswith("/logs"):
            framed = b"\x01\x00\x00\x00\x00\x00\x00\x19" + b"2026-09-14 pool exhausted"

            return httpx.Response(200, content=framed)

        if path.endswith("/stats"):
            return httpx.Response(
                200,
                json={
                    "memory_stats": {"usage": 1_932_735_283, "limit": 2_147_483_648},
                    "cpu_stats": {
                        "cpu_usage": {"total_usage": 200},
                        "system_cpu_usage": 2000,
                        "online_cpus": 2,
                    },
                    "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 1000},
                },
            )

        if path.endswith("/create"):
            return httpx.Response(201, json={"Id": "new-id"})

        return httpx.Response(204)


@pytest.fixture(autouse=True)
def daemon(monkeypatch):  # noqa: ANN001, ANN201
    """Every test gets a fake daemon and a configured approval secret."""
    fake = FakeDaemon()
    config.settings.cache_clear()
    monkeypatch.setenv("APPROVAL_SECRET", SECRET)
    monkeypatch.setenv("CONTAINER_PREFIX", "sentinel-")

    monkeypatch.setattr(
        docker,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler), base_url="http://localhost/v1.43"
        ),
    )

    yield fake

    config.settings.cache_clear()


# ---------------------------------------------------------------------- read-only ----


async def test_only_this_stacks_containers_are_listed() -> None:
    listed = await docker.list_containers()

    assert listed["found"] == 1
    assert listed["items"][0]["name"] == "sentinel-orders"
    assert listed["running"] == 1


async def test_the_restart_count_and_the_environment_come_back() -> None:
    """The two signals a configuration fault shows up in and a service log does not."""
    status = await docker.get_container_status("sentinel-orders")

    assert status["restart_count"] == 11
    assert status["health"] == "healthy"
    assert "MaxPoolSize=20" in status["environment"]["ConnectionStrings__Default"]


async def test_container_logs_are_demultiplexed() -> None:
    """Docker frames its log stream; without unframing, every line arrives with control bytes."""
    logs = await docker.get_container_logs("sentinel-orders")

    assert logs["items"] == ["2026-09-14 pool exhausted"]


async def test_memory_is_reported_against_the_limit() -> None:
    """"1.9 GB" says nothing; "1.9 of 2.0, and an OOM kill is next" is the leak scenario."""
    stats = await docker.get_container_stats("sentinel-orders")

    assert stats["memory_percent"] == 90.0
    assert stats["cpu_percent"] == 20.0
    assert "OOM" in stats["restart_hint"]


async def test_a_container_outside_this_stack_is_refused(daemon: FakeDaemon) -> None:
    """The daemon has no notion of "this project", so the boundary is drawn by name."""
    for call in (
        docker.get_container_status("postgres-prod"),
        docker.get_container_logs("some-other-thing"),
        docker.get_container_stats("../etc"),
    ):
        outcome = await call

        assert "not a container this server manages" in outcome["error"]

    assert daemon.requests == [], "nothing refused should have reached the daemon"


# --------------------------------------------------------------------- destructive ----


async def test_a_restart_needs_an_approval_for_that_container(daemon: FakeDaemon) -> None:
    approved = await docker.restart_container(
        name="sentinel-orders",
        approval_token=issue(RESTART, {"name": "sentinel-orders"}),
    )

    assert approved["approved"] is True
    assert approved["restarted"] is True
    assert approved["restart_count_before"] == 11
    assert approved["approval"]["approved_by"] == "operator@sentinel"
    assert any(
        method == "POST" and path.endswith("/restart") for method, path, _ in daemon.requests
    )


async def test_an_approval_for_one_container_does_not_restart_another(daemon: FakeDaemon) -> None:
    """The attack the whole scheme exists to refuse, checked on the server that would do it."""
    refused = await docker.restart_container(
        name="sentinel-payments",
        approval_token=issue(RESTART, {"name": "sentinel-orders"}),
    )

    assert refused["approved"] is False
    assert "different arguments" in refused["error"]
    assert not any(path.endswith("/restart") for _, path, _ in daemon.requests)


async def test_an_approval_for_another_tool_does_not_restart_anything() -> None:
    refused = await docker.restart_container(
        name="sentinel-orders",
        approval_token=issue("docker-mcp/get_container_logs", {"name": "sentinel-orders"}),
    )

    assert refused["approved"] is False
    assert "is for 'docker-mcp/get_container_logs'" in refused["error"]


async def test_an_expired_approval_is_refused() -> None:
    refused = await docker.restart_container(
        name="sentinel-orders",
        approval_token=issue(RESTART, {"name": "sentinel-orders"}, ttl=-1),
    )

    assert refused["approved"] is False
    assert "expired" in refused["error"].casefold()


async def test_a_token_signed_with_another_secret_is_refused() -> None:
    """The AI service verified first. This is the check that matters if something else calls."""
    refused = await docker.restart_container(
        name="sentinel-orders",
        approval_token=issue(RESTART, {"name": "sentinel-orders"}, secret="not-our-secret"),
    )

    assert refused["approved"] is False
    assert "signature" in refused["error"]


async def test_a_refusal_is_a_result_rather_than_an_exception() -> None:
    """The agent has to read "this was not approved" as an outcome it can report."""
    refused = await docker.restart_container(name="sentinel-orders", approval_token="nonsense")

    assert refused["approved"] is False
    assert refused["tool"] == RESTART
    assert "approve" in refused["remediation"].casefold()


async def test_an_environment_change_keeps_what_it_was_not_asked_to_change(
    daemon: FakeDaemon,
) -> None:
    """Every part of the new container comes from the old one's inspect output but the variables."""
    arguments = {
        "name": "sentinel-orders",
        "environment": {"ConnectionStrings__Default": "Host=postgres;MaxPoolSize=200"},
    }
    applied = await docker.update_env_and_restart(
        **arguments, approval_token=issue(UPDATE, arguments)
    )

    assert applied["applied"] is True
    assert applied["changed"]["ConnectionStrings__Default"]["to"].endswith("MaxPoolSize=200")

    created = next(
        request for request in daemon.requests if request[1].endswith("/containers/create")
    )

    assert created[2]["name"] == "sentinel-orders"


async def test_a_failed_recreate_leaves_the_original_running(monkeypatch) -> None:  # noqa: ANN001
    """The one path that must not make the incident worse."""
    fake = FakeDaemon(containers_create=httpx.Response(500, json={"message": "no such image"}))
    monkeypatch.setattr(
        docker,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handler), base_url="http://localhost/v1.43"
        ),
    )

    arguments = {"name": "sentinel-orders", "environment": {"A": "1"}}
    outcome = await docker.update_env_and_restart(
        **arguments, approval_token=issue(UPDATE, arguments)
    )

    assert outcome["applied"] is False
    assert "left running" in outcome["note"]

    renames = [params.get("name") for _, path, params in fake.requests if "rename" in path]

    assert renames == ["sentinel-orders-replacing", "sentinel-orders"], "the name must go back"


async def test_nothing_destructive_runs_without_a_configured_secret(monkeypatch) -> None:  # noqa: ANN001
    """The closed default, on the server that holds the socket."""
    config.settings.cache_clear()
    monkeypatch.setenv("APPROVAL_SECRET", "")

    refused = await docker.restart_container(
        name="sentinel-orders", approval_token=issue(RESTART, {"name": "sentinel-orders"})
    )

    assert refused["approved"] is False
    assert "no approval secret" in refused["error"].casefold()

    config.settings.cache_clear()


async def test_a_destructive_tool_may_not_have_defaulted_parameters() -> None:
    """The trap this rule exists for, measured against a live daemon before it was written.

    The server sees its own defaults already filled in; the person approving never saw them. A
    tool declared `restart(name, approval_token, timeout=10)` hashes `{name, timeout}` here and
    `{name}` there, so every approval fails with "different arguments" — which reads exactly
    like an attack and is a signature.
    """
    from _shared.server import build_server, destructive

    scratch = build_server("scratch-mcp", instructions="x")

    with pytest.raises(TypeError) as refused:

        @destructive(scratch, "restart something")
        async def restart_thing(name: str, approval_token: str, timeout: int = 10) -> dict:
            return {}

    assert "timeout" in str(refused.value)
    assert "approval is bound to the arguments" in str(refused.value)
