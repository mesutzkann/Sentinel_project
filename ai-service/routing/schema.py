"""What a router says, and the fifteen things it can say.

This is the contract in docs/planning.md §8 — ``{intent, requires_rag, requires_mcp, tools[],
target_service?}`` — written as a Pydantic model, because in Phase 8 a 1.5B model has to emit it
as JSON and be scored on how often it gets the shape right.

**The fifteen intents are defined here rather than in the planning document, which said "15
intent" and named two.** They are chosen so that each one implies a *different plan*: if two
intents would make the agent collect the same evidence, they are one intent with two phrasings,
and a router trained to separate them would be learning a distinction that changes nothing.
That is also why they are not the fifteen chaos scenarios — those are conclusions, and they
already live in ``root_causes.category``. An intent is what the *question* is, before anything
has been collected; a scenario code is what the answer turned out to be.

The set matters beyond this module: Phase 8 generates three to four thousand training examples
across these fifteen, so renaming one after that dataset exists means regenerating it. Adding a
sixteenth is cheap; splitting an existing one is not.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Intent(StrEnum):
    """What the question is asking for.

    Ordered roughly from "investigate everything" to "answer from documents", which is also the
    order of how much of the state machine each one runs.
    """

    # The whole machine. "Payment service is erroring, find out why."
    FULL_INVESTIGATION = "FULL_INVESTIGATION"

    # A failure is known and named; the question is what is throwing and how often.
    ERROR_ANALYSIS = "ERROR_ANALYSIS"

    # One signal, asked for directly. These three skip straight to a single collector.
    LOG_QUERY = "LOG_QUERY"
    METRIC_QUERY = "METRIC_QUERY"
    TRACE_QUERY = "TRACE_QUERY"

    # "Why is it slow" — metrics and traces together, and no assumption that anything errored.
    PERFORMANCE_ANALYSIS = "PERFORMANCE_ANALYSIS"

    # Connections, locks, slow statements: the database as a subject rather than a dependency.
    DATABASE_HEALTH = "DATABASE_HEALTH"

    # "What changed?" Commits and deploys correlated against when the trouble started.
    DEPLOYMENT_CHECK = "DEPLOYMENT_CHECK"

    # Where something is implemented, versus what it is currently set to. Different tools:
    # one reads the repository, the other reads the running configuration.
    CODE_LOOKUP = "CODE_LOOKUP"
    CONFIG_LOOKUP = "CONFIG_LOOKUP"

    # Who calls whom. Answerable from traces and from the architecture documents.
    SERVICE_TOPOLOGY = "SERVICE_TOPOLOGY"

    # "Have we seen this before?" Past incidents, by similarity. Phase 9 makes this good.
    HISTORICAL_SIMILARITY = "HISTORICAL_SIMILARITY"

    # "How do I diagnose a deadlock?" A runbook question, with no live system in it.
    KNOWLEDGE_QUESTION = "KNOWLEDGE_QUESTION"

    # "How do we fix this?" Retrieval now; in Phase 10 it is what precedes an approval.
    REMEDIATION_QUESTION = "REMEDIATION_QUESTION"

    # Everything else, including questions too vague to plan for. Deliberately last: a router
    # that is unsure should land here rather than guess, because a wrong plan collects the wrong
    # evidence and the agent then reasons confidently over it.
    GENERAL_QUESTION = "GENERAL_QUESTION"


class RouteDecision(BaseModel):
    """The router's answer, and the whole of what the planner reads.

    ``tools`` is advisory rather than binding. The planner uses it to decide which collectors to
    run, and every call still goes through :class:`mcp_client.policy.McpPolicy`, so a router that
    hallucinates a tool name gets a refusal rather than an execution. That is deliberate: the
    Phase 8 model is 1.5B, and the cheapest way to make its mistakes harmless is to give its
    output no authority it does not need.
    """

    model_config = {"frozen": True}

    intent: Intent

    requires_rag: bool = Field(
        description="Whether the knowledge base should be searched for this question."
    )

    requires_mcp: bool = Field(
        description="Whether any live signal is needed. False for a pure documentation question."
    )

    tools: list[str] = Field(
        default_factory=list,
        description="Qualified tool names the question suggests, e.g. logs-mcp/get_recent_errors.",
    )

    target_service: str | None = Field(
        default=None,
        description="The service the question is about, when it names or implies exactly one.",
    )
