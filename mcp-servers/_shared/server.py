"""Building and running an MCP server, the same way seven times.

The annotations set here are what the AI service's policy layer uses to tell a read-only tool
from a destructive one, so they are a security boundary rather than documentation.

Since Phase 10 there are destructive tools, and :func:`destructive` is the whole of how they
differ: the annotation says so, and **the tool verifies the approval itself** before its body
runs. The AI service's policy has already refused the call without a valid approval — this is
the second check, in the container that would do the thing, because these servers listen on a
compose network and a tool that restarts containers must not be callable by whatever else can
reach the port.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from _shared.approval import APPROVAL_ARGUMENT, verify
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


def destructive(
    server: MCPServer,
    description: str,
    *,
    idempotent: bool = False,
) -> Callable[[F], F]:
    """Registers a tool that changes the running system.

    The wrapped function must take ``approval_token`` and every other argument by keyword. Before
    its body runs, the token is checked against *this* tool's qualified name and *these*
    arguments — so an approval to restart one container does not restart another, and an approved
    call cannot gain an argument in flight.

    A refusal comes back as a normal result rather than an exception: the agent has to be able to
    read "this was not approved" as an outcome and report it, and an MCP error would reach the
    model as a broken tool instead.
    """

    def decorator(func: F) -> F:
        qualified = f"{server.name}/{func.__name__}"
        _refuse_defaults(qualified, func)

        @functools.wraps(func)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            token = kwargs.get(APPROVAL_ARGUMENT)
            check = verify(token, tool=qualified, arguments=kwargs)

            if not check.valid:
                logger.warning("Refused %s: %s", qualified, check.reason)

                return {
                    "approved": False,
                    "tool": qualified,
                    "error": check.reason,
                    # Named so the agent can say what to do about it rather than retrying a call
                    # that will never be allowed.
                    "remediation": "A human has to approve this specific action first.",
                }

            logger.info(
                "Executing %s under approval %s (%s)",
                qualified,
                check.recommendation_id,
                check.approved_by or "unattributed",
            )

            outcome = await func(*args, **kwargs)

            if isinstance(outcome, dict):
                # Every destructive result carries the approval it ran under. `tool_calls`
                # records it, and an execution nobody can trace to an approval is one nobody
                # approved.
                return {
                    "approved": True,
                    "approval": {
                        "recommendation_id": check.recommendation_id,
                        "approved_by": check.approved_by,
                    },
                    **outcome,
                }

            return outcome

        return server.tool(
            description=description,
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=True,
                idempotent_hint=idempotent,
                open_world_hint=True,
            ),
        )(guarded)

    return decorator


def _refuse_defaults(qualified: str, func: Callable[..., Any]) -> None:
    """A destructive tool may not have defaulted parameters. Checked at import, loudly.

    The approval binds a hash of the arguments, and the server sees its own defaults already
    filled in while the person approving never saw them: a tool declared
    `restart_container(name, approval_token, timeout=10)` hashes `{name, timeout}` here and
    `{name}` there, and every approval fails with "different arguments" — which reads exactly
    like an attack and is a signature. Measured the hard way against a live daemon.

    So the rule is that a destructive tool takes exactly what it was approved for. A value that
    wants a default belongs in a module constant, where it is part of the tool rather than part
    of the call.
    """
    defaulted = [
        name
        for name, parameter in inspect.signature(func).parameters.items()
        if parameter.default is not inspect.Parameter.empty
    ]

    if defaulted:
        raise TypeError(
            f"{qualified} is destructive and has defaulted parameter(s) {defaulted}. "
            "An approval is bound to the arguments it was given, and a default the approver "
            "never saw changes the hash. Make them required, or make them constants."
        )


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
