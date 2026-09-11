"""Where the agent is assembled, and the only place that knows the whole machine.

Until now the nodes existed and nothing put them together: the reasoning benchmark builds the
six states it measures and the tests build one node at a time. That was fine while the phase was
being written and it is not a composition root — it is two partial ones that can disagree.

**Every collector is registered, not only the ones the plan asked for.** ``PLAN`` picks a subset
of collectors from the route, and ``COLLECT_ADDITIONAL_EVIDENCE`` can then send the machine to a
collector the plan never had — that is its whole purpose, closing a gap a hypothesis exposed. A
registry built from the plan would make that transition land on a state with no node, which the
runner correctly calls a defect and ends the run for. The machine is therefore built from
:data:`agents.states.State`, and :func:`build_nodes` is checked against it.

**The dependencies are passed in, not constructed here.** The provider, the MCP client, the
retriever and the router are all seams with more than one implementation in this project — Phase
8 swaps the router for a fine-tuned model, the benchmark swaps the provider for another model,
and the tests swap all four for fakes. A builder that constructed them would be a builder nothing
could test without a running stack.
"""

from __future__ import annotations

from agents.nodes import (
    CheckDeploymentsNode,
    CollectAdditionalEvidenceNode,
    CollectDatabaseNode,
    CollectLogsNode,
    CollectMetricsNode,
    CollectTracesNode,
    GenerateHypothesesNode,
    InspectCodeNode,
    PlanNode,
    RankHypothesesNode,
    RecommendFixNode,
    SearchHistoryNode,
    SelectRootCauseNode,
    UnderstandIncidentNode,
    ValidateNode,
)
from agents.nodes.reasoning import DEFAULT_MAX_ATTEMPTS
from agents.nodes.search_history import DEFAULT_DOCUMENTS
from agents.state_machine import DEFAULT_MAX_TRANSITIONS, Emitter, Node, StateMachine
from agents.states import TERMINAL_STATES, State
from llm.base import LocalLlmProvider
from mcp_client.client import McpClient
from rag.retrievers import Retriever
from routing.base import Router


class IncompleteMachineError(RuntimeError):
    """A state the machine can reach has no node.

    Raised at build time rather than discovered at run time. The runner already treats an
    unimplemented state as a defect and ends the investigation in FAILED — correctly, because a
    conclusion drawn past a collector that never ran is drawn from evidence that does not exist.
    This makes that defect impossible to deploy instead of visible once it happens.
    """


def build_nodes(
    *,
    provider: LocalLlmProvider,
    mcp_client: McpClient,
    retriever: Retriever,
    router: Router,
    prompt_version: str = "v1",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    history_documents: int = DEFAULT_DOCUMENTS,
) -> dict[State, Node]:
    """One node per non-terminal state.

    ``prompt_version`` reaches every reasoning node at once, because Phase 11 compares prompt
    versions across whole runs: a build where one node was on v2 and the rest on v1 would produce
    a number that belongs to neither version.
    """
    reasoning = {"prompt_version": prompt_version, "max_attempts": max_attempts}

    nodes: list[Node] = [
        UnderstandIncidentNode(),
        PlanNode(router),
        CollectLogsNode(mcp_client),
        CollectMetricsNode(mcp_client),
        CollectTracesNode(mcp_client),
        CollectDatabaseNode(mcp_client),
        CheckDeploymentsNode(mcp_client),
        InspectCodeNode(mcp_client),
        SearchHistoryNode(retriever, documents=history_documents),
        GenerateHypothesesNode(provider, **reasoning),
        CollectAdditionalEvidenceNode(),
        RankHypothesesNode(),
        SelectRootCauseNode(provider, **reasoning),
        ValidateNode(provider, **reasoning),
        RecommendFixNode(provider, **reasoning),
    ]

    registry = {node.state: node for node in nodes}
    check_complete(registry)

    return registry


def check_complete(registry: dict[State, Node]) -> None:
    """Refuse a registry that cannot cover every state the machine can reach.

    Reachability is not computed from the plan, because the plan is not the whole graph:
    COLLECT_ADDITIONAL_EVIDENCE can send the machine to any collector, and a back-loop can reach
    a reasoning state a second time. Every non-terminal state is therefore required, which is
    both the correct condition and the only one that does not need updating whenever an edge
    moves.
    """
    missing = [state for state in State if state not in TERMINAL_STATES and state not in registry]

    if missing:
        raise IncompleteMachineError(
            f"no node implements {', '.join(state.value for state in missing)}"
        )


def build_machine(
    *,
    provider: LocalLlmProvider,
    mcp_client: McpClient,
    retriever: Retriever,
    router: Router,
    emit: Emitter | None = None,
    prompt_version: str = "v1",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    history_documents: int = DEFAULT_DOCUMENTS,
    max_transitions: int = DEFAULT_MAX_TRANSITIONS,
) -> StateMachine:
    """The whole agent, ready to run one investigation.

    A new machine per investigation rather than one for the process. It is cheap — the expensive
    things are the provider and the client, and those are shared — and the emitter belongs to a
    run: a process-wide machine would have to be told where to send each event, which is the
    argument the runner deliberately does not take.
    """
    return StateMachine(
        build_nodes(
            provider=provider,
            mcp_client=mcp_client,
            retriever=retriever,
            router=router,
            prompt_version=prompt_version,
            max_attempts=max_attempts,
            history_documents=history_documents,
        ),
        emit=emit,
        max_transitions=max_transitions,
    )
