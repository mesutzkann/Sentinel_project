"""Calling a tool, once policy has allowed it."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mcp_client.policy import McpPolicy, PolicyResult
from mcp_client.registry import McpServerConfig, McpToolRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolCallResult:
    """The outcome of one call, in the shape ``tool_calls`` rows are written from."""

    tool: str
    server: str
    arguments: dict[str, Any]
    success: bool
    latency_ms: int
    content: Any = None
    error: str | None = None


class McpClient:
    """Runs tool calls against the discovered servers.

    A connection per call rather than a pooled session. The servers are stateless, an
    investigation makes tens of calls rather than thousands, and a long-lived session would have
    to be reconnected on every server restart — which is exactly what happens when the agent
    restarts a container in Phase 10.
    """

    def __init__(
        self,
        servers: list[McpServerConfig],
        registry: McpToolRegistry,
        policy: McpPolicy,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._urls = {server.name: server.url for server in servers}
        self._registry = registry
        self._policy = policy
        self._timeout = timeout_seconds

    async def call(
        self,
        qualified_name: str,
        arguments: dict[str, Any] | None = None,
        approval_token: str | None = None,
    ) -> ToolCallResult:
        """Evaluates policy, then calls the tool if allowed.

        Policy is checked here rather than by the caller so there is one path to a tool call and
        no way to reach a server without passing through it.
        """
        arguments = arguments or {}
        decision = self._policy.evaluate(qualified_name, approval_token)

        if not decision.allowed:
            return self._refused(qualified_name, arguments, decision)

        tool = decision.tool
        assert tool is not None  # guaranteed by an allowed decision
        url = self._urls[tool.server]

        started = time.perf_counter()

        try:
            async with asyncio.timeout(self._timeout):
                # mcp 2.x yields two streams, not three.
                async with streamable_http_client(url) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        response = await session.call_tool(tool.name, arguments)
        except TimeoutError:
            return ToolCallResult(
                tool=tool.name,
                server=tool.server,
                arguments=arguments,
                success=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{qualified_name} did not answer within {self._timeout:.0f}s.",
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed call, not a crash
            return ToolCallResult(
                tool=tool.name,
                server=tool.server,
                arguments=arguments,
                success=False,
                latency_ms=int((time.perf_counter() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        is_error = bool(getattr(response, "is_error", False))

        return ToolCallResult(
            tool=tool.name,
            server=tool.server,
            arguments=arguments,
            success=not is_error,
            latency_ms=latency_ms,
            content=_unwrap(response),
            error=_unwrap(response) if is_error else None,
        )

    def _refused(
        self,
        qualified_name: str,
        arguments: dict[str, Any],
        decision: PolicyResult,
    ) -> ToolCallResult:
        server, _, tool = qualified_name.partition("/")

        logger.warning("Policy refused %s: %s", qualified_name, decision.decision.value)

        return ToolCallResult(
            tool=tool or qualified_name,
            server=server,
            arguments=arguments,
            success=False,
            latency_ms=0,
            error=decision.reason,
        )


def _unwrap(response: Any) -> Any:
    """Pulls the useful payload out of an MCP result.

    Structured content when the server produced it, since that is already the object the tool
    returned. Otherwise the text blocks, parsed as JSON where they are JSON — a tool that
    returns a JSON string should not reach the model as an escaped string inside a string.
    """
    structured = getattr(response, "structured_content", None)
    if structured:
        return structured

    texts: list[str] = []

    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            texts.append(text)

    if not texts:
        return None

    joined = "\n".join(texts)

    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined
