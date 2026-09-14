"""The policy boundary.

Phase 4 ships no destructive tools, so none of this is exercised by the running system yet. It
is tested now because Phase 10 adds tools that restart containers, apply patches and revert
commits, and the moment to establish that the boundary holds is before anything is standing on
it.
"""

from __future__ import annotations

import pytest

from mcp_client.approval import ApprovalTokens
from mcp_client.policy import ApprovalVerifier, Decision, McpPolicy
from mcp_client.registry import Discovery, McpServerConfig, McpTool, McpToolRegistry


def _registry(*tools: McpTool) -> McpToolRegistry:
    registry = McpToolRegistry([McpServerConfig("test-mcp", "http://test/mcp")])
    registry._discovery = Discovery(tools={t.qualified_name: t for t in tools})
    return registry


def _tool(name: str, *, read_only: bool, destructive: bool) -> McpTool:
    return McpTool(
        name=name,
        server="test-mcp",
        description="A tool.",
        input_schema={"type": "object"},
        read_only=read_only,
        destructive=destructive,
    )


READ_ONLY = _tool("get_logs", read_only=True, destructive=False)
DESTRUCTIVE = _tool("restart_container", read_only=False, destructive=True)


def test_read_only_tools_need_no_approval() -> None:
    policy = McpPolicy(_registry(READ_ONLY))

    assert policy.evaluate("test-mcp/get_logs").allowed


def test_destructive_tools_are_refused_without_a_token() -> None:
    policy = McpPolicy(_registry(DESTRUCTIVE))
    outcome = policy.evaluate("test-mcp/restart_container")

    assert not outcome.allowed
    assert outcome.decision is Decision.DENIED_NEEDS_APPROVAL


def test_destructive_tools_are_refused_with_a_wrong_token() -> None:
    policy = McpPolicy(_registry(DESTRUCTIVE), ApprovalVerifier("the-real-token"))
    outcome = policy.evaluate("test-mcp/restart_container", "not-the-token")

    assert not outcome.allowed
    assert outcome.decision is Decision.DENIED_INVALID_APPROVAL


def test_destructive_tools_are_allowed_with_an_approval_for_that_call() -> None:
    tokens = ApprovalTokens("the-secret")
    policy = McpPolicy(_registry(DESTRUCTIVE), ApprovalVerifier(tokens=tokens))
    token = tokens.issue(
        recommendation_id="rec-1",
        tool="test-mcp/restart_container",
        arguments={"name": "orders"},
    )

    outcome = policy.evaluate("test-mcp/restart_container", token, {"name": "orders"})

    assert outcome.allowed
    assert outcome.approval is not None
    assert outcome.approval.grant.recommendation_id == "rec-1"


def test_an_approval_does_not_carry_to_another_call() -> None:
    """The whole reason the token names the action: approving one restart approves one restart."""
    tokens = ApprovalTokens("the-secret")
    policy = McpPolicy(_registry(DESTRUCTIVE), ApprovalVerifier(tokens=tokens))
    token = tokens.issue(
        recommendation_id="rec-1",
        tool="test-mcp/restart_container",
        arguments={"name": "orders"},
    )

    elsewhere = policy.evaluate("test-mcp/restart_container", token, {"name": "payments"})

    assert not elsewhere.allowed
    assert elsewhere.decision is Decision.DENIED_INVALID_APPROVAL
    assert "different arguments" in elsewhere.reason


def test_no_configured_verifier_means_no_destructive_call_can_be_approved() -> None:
    """The closed default. An unset secret must not become an open door."""
    policy = McpPolicy(_registry(DESTRUCTIVE), ApprovalVerifier(None))

    assert not policy.evaluate("test-mcp/restart_container", "any-token").allowed


def test_an_unknown_tool_is_refused_rather_than_passed_through() -> None:
    """A tool nobody has classified is one whose annotations nobody has read."""
    policy = McpPolicy(_registry(READ_ONLY))
    outcome = policy.evaluate("test-mcp/some_tool_we_never_saw")

    assert outcome.decision is Decision.DENIED_UNKNOWN_TOOL


def test_a_tool_that_did_not_declare_itself_read_only_needs_approval() -> None:
    """A server that forgets its annotations should lose privileges, not gain them."""
    unannotated = _tool("mystery", read_only=False, destructive=False)
    policy = McpPolicy(_registry(unannotated))

    assert not policy.evaluate("test-mcp/mystery").allowed


@pytest.mark.parametrize("token", ["", None])
def test_empty_tokens_are_not_treated_as_present(token: str | None) -> None:
    policy = McpPolicy(_registry(DESTRUCTIVE), ApprovalVerifier("the-real-token"))

    assert not policy.evaluate("test-mcp/restart_container", token).allowed
