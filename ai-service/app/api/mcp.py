"""The ``/mcp`` endpoints: what tools exist, and calling one.

This is the surface the frontend's MCP Tools page reads and, from Phase 7, the surface the agent
uses internally. Both go through the same policy layer.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.config import Settings, settings
from mcp_client.approval import approval_tokens
from mcp_client.client import McpClient
from mcp_client.policy import ApprovalVerifier, McpPolicy
from mcp_client.registry import McpServerConfig, McpToolRegistry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["mcp"])

# One registry for the process. Discovery is a startup cost, not a per-request one, and the
# frontend polls this page.
_registry: McpToolRegistry | None = None


def server_configs(config: Settings) -> list[McpServerConfig]:
    """The six read-only servers. Phase 10 adds docker-mcp and testing-mcp."""
    return [
        McpServerConfig("logs-mcp", config.logs_mcp_url),
        McpServerConfig("metrics-mcp", config.metrics_mcp_url),
        McpServerConfig("traces-mcp", config.traces_mcp_url),
        McpServerConfig("database-mcp", config.database_mcp_url),
        McpServerConfig("git-mcp", config.git_mcp_url),
        McpServerConfig("source-code-mcp", config.source_code_mcp_url),
    ]


def get_registry(config: Annotated[Settings, Depends(settings)]) -> McpToolRegistry:
    global _registry  # noqa: PLW0603 - process-wide, intentionally

    if _registry is None:
        _registry = McpToolRegistry(server_configs(config))

    return _registry


def get_policy(
    registry: Annotated[McpToolRegistry, Depends(get_registry)],
    config: Annotated[Settings, Depends(settings)],
) -> McpPolicy:
    return McpPolicy(registry, ApprovalVerifier(tokens=approval_tokens(config)))


def get_client(
    registry: Annotated[McpToolRegistry, Depends(get_registry)],
    policy: Annotated[McpPolicy, Depends(get_policy)],
    config: Annotated[Settings, Depends(settings)],
) -> McpClient:
    return McpClient(server_configs(config), registry, policy)


# ---------------------------------------------------------------------- contracts ----


class ToolSummary(BaseModel):
    name: str
    server: str
    qualified_name: str
    description: str
    read_only: bool
    destructive: bool
    input_schema: dict[str, Any]


class ServerSummary(BaseModel):
    name: str
    url: str
    reachable: bool
    tool_count: int
    error: str | None = None


class ToolsResponse(BaseModel):
    total_tools: int
    servers: list[ServerSummary]
    tools: list[ToolSummary]


class CallRequest(BaseModel):
    tool: str = Field(description="Qualified name, e.g. `logs-mcp/get_service_logs`.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    approval_token: str | None = Field(
        default=None,
        description="Required for destructive tools. Phase 4 has none; every server is read-only.",
    )


class CallResponse(BaseModel):
    tool: str
    server: str
    success: bool
    latency_ms: int
    content: Any = None
    error: str | None = None


# ---------------------------------------------------------------------- endpoints ----


@router.get("/tools", response_model=ToolsResponse)
async def list_tools(
    registry: Annotated[McpToolRegistry, Depends(get_registry)],
    refresh: bool = False,
) -> ToolsResponse:
    """Every tool the agent can reach, grouped by the server that offers it.

    Discovery runs once and is cached. `refresh=true` re-runs it, which is what you want after
    restarting a server — a server that was down at startup stays absent until something asks
    again.
    """
    if refresh or not registry.tools:
        await registry.discover()

    return ToolsResponse(
        total_tools=len(registry.tools),
        servers=[
            ServerSummary(
                name=s.name,
                url=s.url,
                reachable=s.reachable,
                tool_count=s.tool_count,
                error=s.error,
            )
            for s in registry.servers
        ],
        tools=[
            ToolSummary(
                name=t.name,
                server=t.server,
                qualified_name=t.qualified_name,
                description=t.description,
                read_only=t.read_only,
                destructive=t.destructive,
                input_schema=t.input_schema,
            )
            for t in sorted(registry.tools.values(), key=lambda t: t.qualified_name)
        ],
    )


@router.post("/call", response_model=CallResponse)
async def call_tool(
    request: CallRequest,
    registry: Annotated[McpToolRegistry, Depends(get_registry)],
    client: Annotated[McpClient, Depends(get_client)],
) -> CallResponse:
    """Call one tool.

    A refused or failed call comes back as a 200 with ``success: false`` rather than an error
    status. The caller from Phase 7 is an agent, and a failed tool call is an observation it has
    to reason about — "logs-mcp could not be reached" is evidence, and an exception would throw
    that evidence away.
    """
    if not registry.tools:
        await registry.discover()

    result = await client.call(request.tool, request.arguments, request.approval_token)

    return CallResponse(
        tool=result.tool,
        server=result.server,
        success=result.success,
        latency_ms=result.latency_ms,
        content=result.content,
        error=result.error,
    )


@router.get("/health", response_model=list[ServerSummary])
async def mcp_health(
    registry: Annotated[McpToolRegistry, Depends(get_registry)],
) -> list[ServerSummary]:
    """Re-runs discovery and reports which servers answered."""
    discovery = await registry.discover()

    if discovery.reachable_servers == 0:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No MCP server is reachable. Start them with the `mcp` compose profile.",
        )

    return [
        ServerSummary(
            name=s.name, url=s.url, reachable=s.reachable, tool_count=s.tool_count, error=s.error
        )
        for s in discovery.servers
    ]
