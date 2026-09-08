"""Building and running an MCP server, the same way six times.

Every server here is read-only in Phase 4. The destructive tools listed in the plan arrive in
Phase 10 with an approval token, and the annotations set here are what the AI service's policy
layer uses to tell the two apart — so they are a security boundary, not documentation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from _shared.config import settings

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def build_server(name: str, instructions: str) -> MCPServer:
    """Creates a server with the conventions every SentinelAI server shares.

    ``instructions`` is read by the model, not by a person: it says what this server knows about
    so the agent can choose between servers before it starts choosing between tools.
    """
    return MCPServer(
        name=name,
        instructions=instructions,
        # Duplicate registrations are a copy-paste bug, and a warning is easy to miss in a
        # container log. Left on deliberately.
        warn_on_duplicate_tools=True,
    )


def read_only(server: MCPServer, description: str, *, idempotent: bool = True) -> Callable[[F], F]:
    """Registers a read-only tool.

    The annotations travel to the client, where the registry classifies the tool. A tool that
    observes without changing anything is marked here; anything that changes state has to say so
    and is refused without an approval token.
    """

    def decorator(func: F) -> F:
        return server.tool(
            description=description,
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=idempotent,
                # These tools reach Loki, Prometheus, Jaeger, PostgreSQL and the working tree:
                # the answer depends on the world, not only on the arguments.
                open_world_hint=True,
            ),
        )(func)

    return decorator


def serve(server: MCPServer, port: int) -> None:
    """Runs the server over Streamable HTTP.

    Streamable HTTP rather than stdio because each server is its own container: stdio has no
    meaning across a compose network.

    Stateless, because there is nothing to keep between calls. A tool call is a question about
    the world, and statelessness means a restarted container loses nothing and any replica can
    answer.
    """
    config = settings()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        host=config.host,
    )

    logger.info("%s listening on %s:%d/mcp", server.name, config.host, port)

    uvicorn.run(app, host=config.host, port=port, log_level="warning")
