"""The composition root: that it covers the whole machine, and that it says so when it does not.

One test here matters more than the rest. ``COLLECT_ADDITIONAL_EVIDENCE`` can send the machine to
a collector the plan never included, so a registry built from the plan would be a registry with
holes in it — and the run would end in FAILED on a state that had a node available and was never
given one.
"""

from __future__ import annotations

import pytest

from agents.build import IncompleteMachineError, build_machine, build_nodes, check_complete
from agents.states import COLLECTOR_STATES, TERMINAL_STATES, State
from mcp_client.client import McpClient
from mcp_client.policy import ApprovalVerifier, McpPolicy
from mcp_client.registry import McpServerConfig, McpToolRegistry
from rag.retrievers import Retriever
from routing.rule_router import RuleBasedRouter
from tests.support import ScriptedProvider


class NoResults(Retriever):
    """A retriever with an empty knowledge base. Never called by these tests."""

    @property
    def name(self) -> str:
        return "none"

    async def retrieve(self, query, k, filters=None):  # type: ignore[no-untyped-def]
        raise AssertionError("building the machine must not search anything")


def nodes() -> dict[State, object]:
    registry = McpToolRegistry([McpServerConfig("logs-mcp", "http://localhost:7001/mcp")])

    return build_nodes(
        provider=ScriptedProvider([]),
        mcp_client=McpClient(
            [McpServerConfig("logs-mcp", "http://localhost:7001/mcp")],
            registry,
            McpPolicy(registry, ApprovalVerifier(None)),
        ),
        retriever=NoResults(),
        router=RuleBasedRouter(),
    )


def test_every_non_terminal_state_has_a_node() -> None:
    registered = set(nodes())

    assert registered == {state for state in State if state not in TERMINAL_STATES}


def test_every_collector_is_registered_not_only_the_ones_a_plan_would_name() -> None:
    """COLLECT_ADDITIONAL_EVIDENCE can send the machine to any of them."""
    assert set(COLLECTOR_STATES) <= set(nodes())


def test_each_node_is_filed_under_the_state_it_implements() -> None:
    assert all(state is node.state for state, node in nodes().items())  # type: ignore[attr-defined]


def test_a_registry_with_a_hole_in_it_is_refused() -> None:
    with pytest.raises(IncompleteMachineError) as exc:
        check_complete({State.UNDERSTAND_INCIDENT: object()})  # type: ignore[dict-item]

    assert "PLAN" in str(exc.value)
    assert "COLLECT_DATABASE" in str(exc.value)


def test_a_terminal_state_needs_no_node() -> None:
    """COMPLETED, NEEDS_HUMAN and FAILED are where the run stops, not work to be done."""
    assert not set(nodes()) & TERMINAL_STATES


def test_the_machine_builds_and_starts_where_the_happy_path_does() -> None:
    registry = McpToolRegistry([])
    machine = build_machine(
        provider=ScriptedProvider([]),
        mcp_client=McpClient([], registry, McpPolicy(registry, ApprovalVerifier(None))),
        retriever=NoResults(),
        router=RuleBasedRouter(),
    )

    assert machine._start is State.UNDERSTAND_INCIDENT  # noqa: SLF001 - the start is the contract
