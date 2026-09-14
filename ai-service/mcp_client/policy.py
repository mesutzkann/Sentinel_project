"""What the agent is allowed to call, and on whose authority.

The rule is one sentence: a destructive tool needs a valid approval token, and a read-only tool
does not. Everything here exists to make that rule impossible to get around by accident.

Two design choices are worth stating, because both are the opposite of the convenient option:

* An unknown tool is refused. Not "allowed because we have no reason to block it" — a tool the
  registry has never seen is one whose annotations nobody has read, and its name alone says
  nothing about what it does.
* A tool that has not declared itself read-only is treated as destructive. A server that forgets
  its annotations should lose the ability to be called without approval, not gain the ability to
  be called without it.

Phase 4 has no destructive tools at all; every server is read-only. This layer exists now
because Phase 10 adds tools that restart containers, apply patches and revert commits, and the
boundary they will run against should be written and tested before anything needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from mcp_client.approval import ApprovalCheck, ApprovalTokens
from mcp_client.registry import McpTool, McpToolRegistry

logger = logging.getLogger(__name__)


class Decision(StrEnum):
    ALLOWED = "allowed"
    DENIED_UNKNOWN_TOOL = "denied_unknown_tool"
    DENIED_NEEDS_APPROVAL = "denied_needs_approval"
    DENIED_INVALID_APPROVAL = "denied_invalid_approval"


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: Decision
    reason: str
    tool: McpTool | None = None

    #: Present whenever an approval token was examined, valid or not. What a destructive call is
    #: recorded against: `tool_calls.approval_id` in docs/planning.md §4.1 is this grant's
    #: recommendation, and an execution nobody can trace to an approval is one nobody approved.
    approval: ApprovalCheck | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOWED


class ApprovalVerifier:
    """Checks that an approval token authorises the call being made.

    Phase 4 shipped this as a shared-secret equality check, which proved the path and authorised
    everything: a token meaning "a human approved something" would let an approval of "restart
    the orders container" be replayed as "drop the payments table". Since Phase 10 the token
    names the action — see :mod:`mcp_client.approval` — and the tool and arguments of the actual
    call are what it is checked against.

    The default is still closed. With no secret configured every destructive call is refused,
    because an unset secret must not quietly become an open door.
    """

    def __init__(self, secret: str | None = None, *, tokens: ApprovalTokens | None = None) -> None:
        self._tokens = tokens or ApprovalTokens(secret)

    def check(
        self,
        token: str | None,
        *,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> ApprovalCheck:
        return self._tokens.verify(token, tool=tool, arguments=arguments)


class McpPolicy:
    """Decides whether a call may proceed."""

    def __init__(self, registry: McpToolRegistry, verifier: ApprovalVerifier | None = None) -> None:
        self._registry = registry
        self._verifier = verifier or ApprovalVerifier()

    def evaluate(
        self,
        qualified_name: str,
        approval_token: str | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> PolicyResult:
        """Args:
        qualified_name: ``server/tool``, e.g. ``logs-mcp/get_service_logs``.
        approval_token: Required for destructive tools, ignored for read-only ones.
        arguments: What the tool is about to be called with. Part of what the approval is
            checked against: an approval is for an action, and a restart of one container is
            not a restart of another.
        """
        tool = self._registry.get(qualified_name)

        if tool is None:
            return PolicyResult(
                Decision.DENIED_UNKNOWN_TOOL,
                (
                    f"'{qualified_name}' is not a discovered tool. Names are "
                    "server/tool, and only tools the registry found can be called."
                ),
            )

        if not tool.destructive and tool.read_only:
            return PolicyResult(Decision.ALLOWED, "Read-only tool.", tool)

        if approval_token is None:
            return PolicyResult(
                Decision.DENIED_NEEDS_APPROVAL,
                (
                    f"'{qualified_name}' changes state and needs an approval token. "
                    "A human approves the specific action before it can run."
                ),
                tool,
            )

        check = self._verifier.check(approval_token, tool=qualified_name, arguments=arguments)

        if not check.valid:
            # Logged because a rejected token is either a bug or an attempt, and both are worth
            # seeing. The token itself is never logged; the reason it failed is.
            logger.warning(
                "Rejected approval for %s: %s", qualified_name, check.failure or "invalid"
            )

            return PolicyResult(
                Decision.DENIED_INVALID_APPROVAL,
                f"The approval for '{qualified_name}' is not valid: {check.reason}",
                tool,
                check,
            )

        return PolicyResult(Decision.ALLOWED, "Approved destructive tool.", tool, check)
