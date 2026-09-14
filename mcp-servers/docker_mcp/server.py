"""docker-mcp: what the containers are doing, and — with an approval — making them do it again.

This is the first server in the project that can change the running system, and the only one
given the daemon socket. Three things follow from that and none of them are optional:

* **The socket is the blast radius.** A container with `/var/run/docker.sock` mounted can do
  anything to the host's Docker, including to itself and to containers that have nothing to do
  with this project. So every tool here refuses a container whose name does not start with
  `CONTAINER_PREFIX`, and the refusal is by name rather than by label because a name is what an
  approval a person read said.
* **Destructive tools verify their own approval.** `_shared.server.destructive` checks the token
  against this tool and these arguments before the body runs. The AI service checked it too;
  that one is the caller, this one is the thing being called.
* **The read-only half is genuinely read-only.** `list_containers`, `get_container_status`,
  `get_container_logs` and `get_container_stats` need no approval and cannot change anything, so
  an investigation can ask what is running without anybody being asked to click approve.

The daemon is reached over HTTP rather than through the `docker` SDK: the API surface used here
is five endpoints, the SDK is a large dependency that would go into every one of these servers'
shared image, and the socket transport is twenty lines of httpx.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from _shared.config import settings
from _shared.results import result, truncated
from _shared.server import build_server, destructive, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "docker-mcp",
    instructions=(
        "The containers this system runs in: what is up, what restarted, what a container's "
        "environment is set to, and its recent output. Restarting a container and changing its "
        "environment are possible here and need a human's approval first — everything else is "
        "read-only. Use this when a fault looks like a process rather than like code: a "
        "container that keeps restarting, a setting that is wrong in the environment rather "
        "than in the file, memory climbing towards a limit."
    ),
)

#: The daemon's API version, pinned. An unpinned client negotiates, and a tool whose response
#: shape depends on which Docker the host happens to run is a tool that breaks on somebody
#: else's machine.
API_VERSION = "v1.43"

#: Log lines returned at most. Container logs go into a prompt like every other tool result.
MAX_LOG_LINES = 200

#: Seconds a container gets to stop before it is killed. A constant rather than a parameter:
#: an approval is bound to the arguments it was given, and a defaulted parameter is one the
#: approver never saw — see `_shared.server._refuse_defaults`.
STOP_TIMEOUT = 10

#: How long to wait for the daemon. A restart takes seconds; a stuck daemon must not hold an
#: investigation open for a minute.
DAEMON_TIMEOUT = 30.0


class ContainerRefused(ValueError):
    """A container this server will not touch."""


def _client() -> httpx.AsyncClient:
    """An httpx client over the Docker socket, or over TCP when one is configured."""
    host = settings().docker_host

    if host.startswith("unix://"):
        transport = httpx.AsyncHTTPTransport(uds=host.removeprefix("unix://"))

        return httpx.AsyncClient(
            transport=transport, base_url=f"http://localhost/{API_VERSION}", timeout=DAEMON_TIMEOUT
        )

    return httpx.AsyncClient(
        base_url=f"{host.rstrip('/')}/{API_VERSION}", timeout=DAEMON_TIMEOUT
    )


def _check_name(name: str) -> str:
    """Refuses anything outside this project, by name.

    The daemon has no notion of "this stack", so the boundary is drawn here. By prefix rather
    than by compose label because the name is what a person read on an approval screen, and the
    thing that gets restarted has to be the thing that was named.
    """
    prefix = settings().container_prefix
    cleaned = name.strip().lstrip("/")

    if not cleaned:
        raise ContainerRefused("No container name was given.")

    if prefix and not cleaned.startswith(prefix):
        raise ContainerRefused(
            f"'{cleaned}' is not a container this server manages. Only containers named "
            f"'{prefix}*' can be inspected or restarted here."
        )

    return cleaned


def _summarise(container: dict[str, Any]) -> dict[str, Any]:
    names = [n.lstrip("/") for n in container.get("Names", [])]

    return {
        "name": names[0] if names else container.get("Id", "")[:12],
        "image": container.get("Image"),
        "state": container.get("State"),
        "status": container.get("Status"),
        "health": (container.get("Status") or "").split("(")[-1].rstrip(")")
        if "(" in (container.get("Status") or "")
        else None,
        "created": container.get("Created"),
    }


@read_only(server, "Every container in this stack, with its state and health.")
async def list_containers() -> dict[str, Any]:
    prefix = settings().container_prefix

    async with _client() as client:
        response = await client.get("/containers/json", params={"all": "true"})
        response.raise_for_status()
        containers = response.json()

    mine = [
        _summarise(container)
        for container in containers
        if any(n.lstrip("/").startswith(prefix) for n in container.get("Names", []))
    ]
    items, was_truncated = truncated(mine, settings().max_results)

    return result(
        found=len(mine),
        window="now",
        items=items,
        truncated_at=settings().max_results if was_truncated else None,
        running=sum(1 for c in mine if c["state"] == "running"),
    )


@read_only(server, "One container's state, restart count, exit code and environment.")
async def get_container_status(name: str) -> dict[str, Any]:
    """The restart count is the reason this tool exists.

    A container that has restarted eleven times in an hour is a different fault from one that is
    simply down, and neither shows up in a log the service wrote — the process that would have
    written it is the one that died.
    """
    try:
        container = _check_name(name)
    except ContainerRefused as exc:
        return {"error": str(exc)}

    async with _client() as client:
        response = await client.get(f"/containers/{container}/json")

        if response.status_code == httpx.codes.NOT_FOUND:
            return {"error": f"No container named '{container}' is on this host."}

        response.raise_for_status()
        detail = response.json()

    state = detail.get("State", {})
    config = detail.get("Config", {})

    return {
        "name": container,
        "state": state.get("Status"),
        "running": bool(state.get("Running")),
        "restart_count": detail.get("RestartCount", 0),
        "exit_code": state.get("ExitCode"),
        "started_at": state.get("StartedAt"),
        "finished_at": state.get("FinishedAt"),
        "health": (state.get("Health") or {}).get("Status"),
        "image": config.get("Image"),
        # The environment is the point for a configuration fault: the value that is wrong is in
        # here rather than in the file the repository holds.
        "environment": _env_map(config.get("Env") or []),
    }


@read_only(server, "The last lines a container wrote, including what it printed while dying.")
async def get_container_logs(name: str, tail: int = 100) -> dict[str, Any]:
    """Not the same signal as logs-mcp.

    Loki holds what the *service* chose to log. This holds what the *container* emitted, which is
    the only place a stack trace from a process that died before its logger flushed can be.
    """
    try:
        container = _check_name(name)
    except ContainerRefused as exc:
        return {"error": str(exc)}

    lines = max(1, min(tail, MAX_LOG_LINES))

    async with _client() as client:
        response = await client.get(
            f"/containers/{container}/logs",
            params={"stdout": "true", "stderr": "true", "tail": str(lines), "timestamps": "true"},
        )

        if response.status_code == httpx.codes.NOT_FOUND:
            return {"error": f"No container named '{container}' is on this host."}

        response.raise_for_status()
        body = _demultiplex(response.content)

    written = [line for line in body.splitlines() if line.strip()]

    return result(found=len(written), window=f"last {lines} line(s)", items=written, name=container)


@read_only(server, "CPU and memory for one container, against its limit.")
async def get_container_stats(name: str) -> dict[str, Any]:
    """Memory against the *limit*, not in megabytes alone.

    "1.9 GB" says nothing; "1.9 of 2.0 GB" is the memory-leak scenario a page away from an OOM
    kill, and the percentage is what makes the two legible as different states.
    """
    try:
        container = _check_name(name)
    except ContainerRefused as exc:
        return {"error": str(exc)}

    async with _client() as client:
        response = await client.get(f"/containers/{container}/stats", params={"stream": "false"})

        if response.status_code == httpx.codes.NOT_FOUND:
            return {"error": f"No container named '{container}' is on this host."}

        response.raise_for_status()
        stats = response.json()

    memory = stats.get("memory_stats", {})
    used = memory.get("usage")
    limit = memory.get("limit")

    # The hint reads the percentage that is reported rather than the raw ratio. A result saying
    # "90.0%" with no warning beside one saying "90.1%" with one is a difference nobody can see
    # and everybody has to explain.
    percent = round(used / limit * 100, 1) if used and limit else None

    return {
        "name": container,
        "cpu_percent": _cpu_percent(stats),
        "memory_mb": round(used / 1_048_576, 1) if used else None,
        "memory_limit_mb": round(limit / 1_048_576, 1) if limit else None,
        "memory_percent": percent,
        "restart_hint": (
            "Memory is within a tenth of the limit; an OOM kill is what happens next."
            if percent is not None and percent >= 90.0
            else None
        ),
    }


@destructive(server, "Restart one container. Needs an approval for this container by name.")
async def restart_container(name: str, approval_token: str) -> dict[str, Any]:
    """The remediation of last resort, and the one most often reached for.

    It fixes a leaked resource and it fixes nothing that will not leak again, so what comes back
    says what it did rather than claiming the incident is over: the restart count before and
    after, and whether the container came up.
    """
    try:
        container = _check_name(name)
    except ContainerRefused as exc:
        return {"error": str(exc), "restarted": False}

    async with _client() as client:
        before = await client.get(f"/containers/{container}/json")

        if before.status_code == httpx.codes.NOT_FOUND:
            return {
                "error": f"No container named '{container}' is on this host.",
                "restarted": False,
            }

        previous = before.json().get("RestartCount", 0)

        response = await client.post(
            f"/containers/{container}/restart", params={"t": str(STOP_TIMEOUT)}
        )
        response.raise_for_status()

        after = (await client.get(f"/containers/{container}/json")).json()

    state = after.get("State", {})

    return {
        "restarted": True,
        "name": container,
        "running": bool(state.get("Running")),
        "state": state.get("Status"),
        "restart_count_before": previous,
        "started_at": state.get("StartedAt"),
        "note": (
            "A restart clears the symptom. Whether it clears the cause is what the verification "
            "step measures."
        ),
    }


@destructive(
    server,
    "Change environment variables on one container and restart it into the new configuration.",
)
async def update_env_and_restart(
    name: str,
    environment: dict[str, str],
    approval_token: str,
) -> dict[str, Any]:
    """The configuration fix: a wrong value in the environment, corrected and applied.

    Docker cannot change a running container's environment, so this recreates it from its own
    configuration with the variables replaced — same image, same name, same networks, same
    mounts, same command. What it deliberately does not do is invent anything: every part of the
    new container comes from the old one's inspect output, and the only difference is the
    variables an approval named.

    **The old container is removed only after the new one exists.** The order matters on the one
    path that goes wrong: if creation fails, the original is still there and still running, and
    the incident is what it was rather than what this tool made it.
    """
    try:
        container = _check_name(name)
    except ContainerRefused as exc:
        return {"error": str(exc), "applied": False}

    if not environment:
        return {"error": "No environment variables were given to change.", "applied": False}

    async with _client() as client:
        inspected = await client.get(f"/containers/{container}/json")

        if inspected.status_code == httpx.codes.NOT_FOUND:
            return {
                "error": f"No container named '{container}' is on this host.",
                "applied": False,
            }

        inspected.raise_for_status()
        detail = inspected.json()

        config = detail.get("Config", {})
        previous = _env_map(config.get("Env") or [])
        merged = {**previous, **environment}

        body = {
            **{k: v for k, v in config.items() if k not in {"Env", "Hostname", "Image"}},
            "Image": config.get("Image"),
            "Env": [f"{key}={value}" for key, value in merged.items()],
            "HostConfig": detail.get("HostConfig", {}),
            "NetworkingConfig": {
                "EndpointsConfig": (detail.get("NetworkSettings", {}) or {}).get("Networks", {})
            },
        }

        # Rename rather than remove: the original keeps running under another name until the
        # replacement is up, so a failed create leaves the stack exactly as it was.
        await client.post(
            f"/containers/{container}/rename", params={"name": f"{container}-replacing"}
        )

        try:
            created = await client.post("/containers/create", params={"name": container}, json=body)
            created.raise_for_status()
            new_id = created.json()["Id"]

            await client.post(f"/containers/{container}/stop", params={"t": "10"})
            started = await client.post(f"/containers/{new_id}/start")
            started.raise_for_status()
        except (httpx.HTTPError, KeyError) as exc:
            # Put the name back. The incident is what it was; this tool has not made it worse.
            await client.post(
                f"/containers/{container}-replacing/rename", params={"name": container}
            )

            return {
                "applied": False,
                "error": f"Could not apply the new environment: {type(exc).__name__}: {exc}",
                "note": "The original container was left running under its own name.",
            }

        await client.delete(f"/containers/{container}-replacing", params={"force": "true"})

    return {
        "applied": True,
        "name": container,
        "changed": {
            key: {"from": previous.get(key), "to": value} for key, value in environment.items()
        },
        "note": (
            "The container was recreated with the new environment. A configuration fix is only "
            "confirmed by measuring the symptom again."
        ),
    }


# -------------------------------------------------------------------------- helpers ----


def _env_map(entries: list[str]) -> dict[str, str]:
    """``["A=1", "B=2"]`` to a mapping, keeping the first `=` as the separator."""
    pairs: dict[str, str] = {}

    for entry in entries:
        key, separator, value = entry.partition("=")

        if separator:
            pairs[key] = value

    return pairs


def _cpu_percent(stats: dict[str, Any]) -> float | None:
    """Docker reports counters; the percentage is the delta between two of them."""
    cpu = stats.get("cpu_stats", {})
    previous = stats.get("precpu_stats", {})

    used = cpu.get("cpu_usage", {}).get("total_usage")
    used_before = previous.get("cpu_usage", {}).get("total_usage")
    system = cpu.get("system_cpu_usage")
    system_before = previous.get("system_cpu_usage")

    if None in (used, used_before, system, system_before):
        return None

    delta = used - used_before
    system_delta = system - system_before

    if system_delta <= 0 or delta < 0:
        return None

    cores = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or []) or 1

    return round(delta / system_delta * cores * 100, 1)


def _demultiplex(raw: bytes) -> str:
    """Docker's log stream carries an 8-byte header per frame unless the container has a TTY.

    Without this, every line comes back with control bytes in front of it — which reads to a
    model as corrupted output and to a person as a bug in the tool.
    """
    if not raw:
        return ""

    # A TTY container's stream is plain. The header's first byte is the stream type (1 or 2) and
    # bytes 1-3 are always zero, which plain text effectively never is.
    if raw[0] not in (0, 1, 2) or raw[1:4] != b"\x00\x00\x00":
        return raw.decode("utf-8", errors="replace")

    out: list[str] = []
    offset = 0

    while offset + 8 <= len(raw):
        size = int.from_bytes(raw[offset + 4 : offset + 8], "big")
        out.append(raw[offset + 8 : offset + 8 + size].decode("utf-8", errors="replace"))
        offset += 8 + size

    return "".join(out)


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
