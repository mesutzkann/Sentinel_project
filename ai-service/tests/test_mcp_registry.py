"""Tool discovery and classification.

The classification is what the policy layer acts on, so the defaults matter more than they look:
these tests pin down what happens when a server says nothing about a tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_client.registry import McpServerConfig, McpTool, McpToolRegistry, ServerStatus, _to_tool


@dataclass
class FakeAnnotations:
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None


@dataclass
class FakeTool:
    name: str
    description: str | None = "Does a thing."
    input_schema: dict | None = None
    annotations: FakeAnnotations | None = None


def test_a_read_only_annotation_is_honoured() -> None:
    tool = _to_tool("logs-mcp", FakeTool("get_logs", annotations=FakeAnnotations(True, False)))

    assert tool.read_only and not tool.destructive


def test_a_destructive_annotation_is_honoured() -> None:
    tool = _to_tool("docker-mcp", FakeTool("restart", annotations=FakeAnnotations(False, True)))

    assert tool.destructive and not tool.read_only


def test_a_tool_with_no_annotations_is_treated_as_destructive() -> None:
    """The careful direction.

    The alternative — assuming read-only when a server says nothing — means a server that
    forgets its annotations silently gains the ability to be called without approval.
    """
    tool = _to_tool("mystery-mcp", FakeTool("do_something"))

    assert not tool.read_only
    assert tool.destructive


def test_qualified_names_keep_same_named_tools_apart() -> None:
    """Two servers legitimately expose get_error_rate, measuring different things."""
    from_logs = _to_tool("logs-mcp", FakeTool("get_error_rate"))
    from_metrics = _to_tool("metrics-mcp", FakeTool("get_error_rate"))

    assert from_logs.qualified_name != from_metrics.qualified_name
    assert from_logs.qualified_name == "logs-mcp/get_error_rate"


def _registry_with(tools: list[McpTool], statuses: list[ServerStatus]) -> McpToolRegistry:
    from mcp_client.registry import Discovery

    registry = McpToolRegistry([McpServerConfig("a", "http://a/mcp")])
    registry._discovery = Discovery(
        tools={t.qualified_name: t for t in tools}, servers=statuses
    )
    return registry


def test_tools_are_grouped_by_server() -> None:
    tools = [
        _to_tool("logs-mcp", FakeTool("b")),
        _to_tool("logs-mcp", FakeTool("a")),
        _to_tool("git-mcp", FakeTool("c")),
    ]

    grouped = _registry_with(tools, []).by_server()

    assert list(grouped) == ["git-mcp", "logs-mcp"]
    assert [t.name for t in grouped["logs-mcp"]] == ["a", "b"]


def test_an_unreachable_server_does_not_hide_the_others() -> None:
    """Five working servers are a usable agent; one down server must not empty the registry."""
    statuses = [
        ServerStatus("logs-mcp", "http://logs/mcp", reachable=True, tool_count=1),
        ServerStatus("git-mcp", "http://git/mcp", reachable=False, error="connection refused"),
    ]
    registry = _registry_with([_to_tool("logs-mcp", FakeTool("get_logs"))], statuses)

    assert len(registry.tools) == 1
    assert registry.get("logs-mcp/get_logs") is not None
    assert [s.name for s in registry.servers if not s.reachable] == ["git-mcp"]


async def test_discovery_reports_an_unreachable_server_rather_than_raising() -> None:
    """A server that is down is a status, not an exception: startup must survive it."""
    registry = McpToolRegistry(
        [McpServerConfig("nowhere-mcp", "http://127.0.0.1:9/mcp")], timeout_seconds=2
    )

    discovery = await registry.discover()

    assert discovery.reachable_servers == 0
    assert discovery.servers[0].error
    assert discovery.tools == {}
