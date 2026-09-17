"""Discovering what the agent can do, and keeping track of what each thing is allowed to do.

The registry connects to every configured MCP server at startup, asks each for its tools, and
classifies them by the annotations the server sent. That classification is the input to the
policy layer, so it is a security boundary rather than a catalogue: a tool the server calls
destructive is one this process refuses to call without an approval token.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class McpServerConfig:
    """One server the agent can reach."""

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class McpTool:
    """A tool as its server described it."""

    name: str
    server: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    destructive: bool

    @property
    def qualified_name(self) -> str:
        """``logs-mcp/get_service_logs``. Tool names are only unique within a server.

        Two servers legitimately expose ``get_error_rate`` — one measures log lines and the
        other measures requests — so the server has to be part of the identity or the agent
        cannot tell them apart.
        """
        return f"{self.server}/{self.name}"


@dataclass
class ServerStatus:
    """Whether a server answered, and what it offered."""

    name: str
    url: str
    reachable: bool
    tool_count: int = 0
    error: str | None = None


@dataclass
class Discovery:
    """The result of one discovery pass."""

    tools: dict[str, McpTool] = field(default_factory=dict)
    servers: list[ServerStatus] = field(default_factory=list)

    @property
    def reachable_servers(self) -> int:
        return sum(1 for s in self.servers if s.reachable)


class McpToolRegistry:
    """Every tool across every server, discovered once and refreshable.

    Discovery is deliberately tolerant of a server being down. Five working servers are a usable
    agent; refusing to start because one is unreachable would make the whole system as available
    as its least available part, and the status of each is reported so a missing tool is
    explainable rather than mysterious.
    """

    def __init__(self, servers: list[McpServerConfig], timeout_seconds: float = 15.0) -> None:
        self._servers = servers
        self._timeout = timeout_seconds
        self._discovery = Discovery()

    @property
    def tools(self) -> dict[str, McpTool]:
        return self._discovery.tools

    @property
    def servers(self) -> list[ServerStatus]:
        return self._discovery.servers

    def get(self, qualified_name: str) -> McpTool | None:
        return self._discovery.tools.get(qualified_name)

    def by_server(self) -> dict[str, list[McpTool]]:
        grouped: dict[str, list[McpTool]] = {}

        for tool in self._discovery.tools.values():
            grouped.setdefault(tool.server, []).append(tool)

        return {
            name: sorted(tools, key=lambda t: t.name)
            for name, tools in sorted(grouped.items())
        }

    async def discover(self) -> Discovery:
        """Asks every server what it offers.

        Servers are queried concurrently: six sequential connections would make startup wait on
        the slowest, and they have nothing to do with each other.
        """
        results = await asyncio.gather(
            *(self._discover_one(server) for server in self._servers),
            return_exceptions=False,
        )

        discovery = Discovery()

        for status, tools in results:
            discovery.servers.append(status)

            for tool in tools:
                discovery.tools[tool.qualified_name] = tool

        self._discovery = discovery

        logger.info(
            "Discovered %d tools across %d of %d servers",
            len(discovery.tools),
            discovery.reachable_servers,
            len(self._servers),
        )

        return discovery

    async def _discover_one(
        self, server: McpServerConfig
    ) -> tuple[ServerStatus, list[McpTool]]:
        try:
            async with asyncio.timeout(self._timeout):
                # mcp 2.x yields two streams, not three.
                async with (
                    streamable_http_client(server.url) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    listed = await session.list_tools()
        except Exception as exc:  # noqa: BLE001 - one server's failure must not end discovery
            reason = _describe(exc)
            logger.warning("%s at %s is unreachable: %s", server.name, server.url, reason)
            return ServerStatus(server.name, server.url, reachable=False, error=reason), []

        tools = [_to_tool(server.name, raw) for raw in listed.tools]

        return (
            ServerStatus(server.name, server.url, reachable=True, tool_count=len(tools)),
            tools,
        )


def _describe(exc: BaseException) -> str:
    """A readable reason a server could not be reached.

    The transport raises inside a task group, so the exception that arrives here is an
    ExceptionGroup whose str() is empty — recording it directly leaves the UI showing a server
    as unreachable with no reason, which is the one thing this field exists to prevent. The
    group is flattened to its causes, and a type name stands in when a cause has no message.
    """
    if isinstance(exc, BaseExceptionGroup):
        causes = [_describe(sub) for sub in exc.exceptions]
        return "; ".join(dict.fromkeys(causes))

    message = str(exc).strip()

    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _to_tool(server: str, raw: Any) -> McpTool:
    """Maps an SDK tool onto ours, deciding read-only versus destructive.

    The defaults are the careful direction. An absent annotation means the server did not say,
    and a tool that has not declared itself read-only is treated as though it changes something
    — the alternative is that a server which forgets its annotations silently gets write access.
    """
    annotations = getattr(raw, "annotations", None)

    read_only = bool(getattr(annotations, "read_only_hint", False))
    destructive = bool(getattr(annotations, "destructive_hint", not read_only))

    return McpTool(
        name=raw.name,
        server=server,
        description=raw.description or "",
        input_schema=raw.input_schema or {},
        read_only=read_only,
        destructive=destructive,
    )
