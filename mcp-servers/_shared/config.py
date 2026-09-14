"""Settings shared by every MCP server.

One class rather than one per server: the servers run as six containers from one image, and a
single set of names means the compose file does not have to remember which variable belongs to
which entrypoint.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Where the server binds. 0.0.0.0 inside a container is correct; the published port is
    # bound to loopback by compose, so this is not an exposure.
    host: str = "0.0.0.0"
    port: int = 7000

    # ---- observability backends, as reachable on the compose network ----
    loki_url: str = "http://loki:3100"
    prometheus_url: str = "http://prometheus:9090"
    jaeger_url: str = "http://jaeger:16686"

    # ---- database ----
    # database-mcp reads pg_stat_* and runs read-only queries, so it connects as the same
    # development user the services use. Phase 10 adds writes behind an approval token.
    postgres_dsn: str = (
        "postgresql://sentinel:sentinel_dev_pw@postgres:5432/sentinel"
    )

    # Every read-only query runs under this ceiling. A tool that hangs stalls an investigation,
    # and no legitimate diagnostic query needs longer.
    query_timeout_ms: int = 5000

    # ---- source and git ----
    # The repository, mounted read-only into the container. git-mcp and source-code-mcp both
    # read it; neither writes until Phase 10.
    repo_root: str = "/repo"

    # Upper bound on how much a single tool returns. Tool output goes into an LLM prompt, so an
    # unbounded result is a context-window failure rather than a helpful answer.
    max_results: int = 100

    # ---- destructive tools ----
    # The key approval tokens are signed with, shared with the backend that issues them and the
    # AI service that verifies them first. Empty means this server refuses every destructive
    # call, which is the right default for a container that can restart other containers.
    approval_secret: str = ""

    # Where docker-mcp reaches the daemon. The socket is mounted read-write into that container
    # and nowhere else: it is the one mount in this project that can change the running system.
    docker_host: str = "unix:///var/run/docker.sock"

    # Only containers whose name starts with this may be restarted or reconfigured. The daemon
    # socket has no notion of "this project", so the boundary is drawn here: an approval is for
    # an action on this stack, and a tool that could restart anything on the host would be a
    # tool whose blast radius is the machine.
    container_prefix: str = "sentinel-"

    http_timeout_seconds: float = 30.0


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
