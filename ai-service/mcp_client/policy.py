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

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOWED


class ApprovalVerifier:
    """Checks an approval token.

    A placeholder in Phase 4 with a deliberately closed default: with no verifier configured,
    every destructive call is refused. Phase 10 replaces this with one that validates a token
    the backend issued when a human clicked approve, and the surrounding policy does not change
    when it does.
    """

    def __init__(self, expected: str | None = None) -> None:
        self._expected = expected

    def verify(self, token: str | None) -> bool:
        if not token or not self._expected:
            return False

        return token == self._expected


class McpPolicy:
    """Decides whether a call may proceed."""

    def __init__(self, registry: McpToolRegistry, verifier: ApprovalVerifier | None = None) -> None:
        self._registry = registry
        self._verifier = verifier or ApprovalVerifier()

    def evaluate(self, qualified_name: str, approval_token: str | None = None) -> PolicyResult:
        """Args:
        qualified_name: ``server/tool``, e.g. ``logs-mcp/get_service_logs``.
        approval_token: Required for destructive tools, ignored for read-only ones.
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

        if not self._verifier.verify(approval_token):
            # Logged because a rejected token is either a bug or an attempt, and both are worth
            # seeing. The token itself is never logged.
            logger.warning("Rejected approval token for %s", qualified_name)

            return PolicyResult(
                Decision.DENIED_INVALID_APPROVAL,
                f"The approval token for '{qualified_name}' is not valid.",
                tool,
            )

        return PolicyResult(Decision.ALLOWED, "Approved destructive tool.", tool)
