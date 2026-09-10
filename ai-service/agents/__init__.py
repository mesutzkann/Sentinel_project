"""The investigation agent: a state machine, its nodes, and the context they share."""

from agents.categories import UNKNOWN_CATEGORY, ScenarioCategory
from agents.confidence import (
    CONFIDENCE_THRESHOLD,
    ConfidenceScore,
    ConfidenceTerm,
    evidence_support,
    hypothesis_margin,
    score_confidence,
)
from agents.context import (
    EvidenceItem,
    EvidenceSource,
    Hypothesis,
    InvestigationContext,
    Recommendation,
    RootCause,
    ToolBudgetExhaustedError,
)
from agents.state_machine import (
    AgentEvent,
    EventType,
    Node,
    RunResult,
    StateMachine,
    Transition,
)
from agents.states import COLLECTOR_STATES, TERMINAL_STATES, State

__all__ = [
    "COLLECTOR_STATES",
    "CONFIDENCE_THRESHOLD",
    "TERMINAL_STATES",
    "UNKNOWN_CATEGORY",
    "AgentEvent",
    "ConfidenceScore",
    "ConfidenceTerm",
    "EventType",
    "EvidenceItem",
    "EvidenceSource",
    "Hypothesis",
    "InvestigationContext",
    "Node",
    "Recommendation",
    "RootCause",
    "RunResult",
    "ScenarioCategory",
    "State",
    "StateMachine",
    "ToolBudgetExhaustedError",
    "Transition",
    "evidence_support",
    "hypothesis_margin",
    "score_confidence",
]
