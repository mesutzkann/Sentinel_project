"""The one thing every node reads and writes.

docs/planning.md §7 says it in a sentence: ``InvestigationContext`` is the single data structure
carrying ``incident, plan, evidence[], hypotheses[], tool_budget, iteration, notes``. Nodes take
it, mutate it, and return a transition; nothing else is passed between states.

That is a deliberate constraint rather than a convenience. A node that needed a private argument
from an earlier node would be a hidden edge in the state machine, and the graph the frontend
draws — and the timeline a human reads to decide whether to trust the conclusion — would no
longer be the whole story of how the conclusion was reached.

**The budget lives here, not in the runner.** ``COLLECT_ADDITIONAL_EVIDENCE`` can send the
machine back to a collector, so the same node runs more than once, and a per-node limit would not
bound anything. Twenty-five tool calls and three iterations are the totals from the planning
document, and they are enforced by the context refusing to spend rather than by every node
remembering to check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from routing.schema import RouteDecision

# From docs/planning.md §7. A budget rather than a timeout because the expensive thing is a tool
# call's effect on the prompt, not its wall clock: twenty-five tool outputs is already more than
# an 8k window holds, which is why the collectors summarise.
DEFAULT_TOOL_BUDGET = 25
DEFAULT_MAX_ITERATIONS = 3


class ToolBudgetExhaustedError(RuntimeError):
    """The investigation asked for a tool call it could not afford.

    Raised rather than returned so that a collector cannot half-notice it. The runner catches it
    and ends the run in ``NEEDS_HUMAN``: an investigation that ran out of budget has evidence and
    no conclusion, which is a different thing from one that failed, and a human should see what
    it did collect.
    """


class EvidenceSource(StrEnum):
    """Where a fact came from.

    Mirrors ``EvidenceSource`` in the backend's ``Enums.cs`` — these values cross the callback
    boundary as strings, so the two lists have to stay in step. The names are the *kind* of
    signal rather than the tool that produced it, because the confidence score pays a diversity
    bonus for facts that agree across sources, and two log tools agreeing is one source agreeing
    with itself.
    """

    LOGS = "logs"
    METRICS = "metrics"
    TRACES = "traces"
    GIT = "git"
    DATABASE = "database"
    SOURCE_CODE = "source_code"
    DOCKER = "docker"
    HISTORICAL_INCIDENT = "historical_incident"
    RAG_DOCUMENT = "rag_document"


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One fact, with what produced it and what it is worth.

    ``raw`` is kept alongside ``summary`` because the summary is what reaches a prompt and the
    raw output is what a human checks when the conclusion looks wrong. A conclusion that cannot
    be traced back to the bytes a tool returned is an assertion.
    """

    source: EvidenceSource
    summary: str
    weight: float = 0.5
    raw: dict[str, Any] | None = None
    tool: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"evidence weight must be in [0, 1], got {self.weight}")


@dataclass(slots=True)
class Hypothesis:
    """A candidate explanation, before or after ranking.

    ``category`` is one of the fifteen chaos scenario codes when the agent can name one, and
    ``None`` when it cannot. Agent evaluation in Phase 11 compares it against the scenario that
    was actually triggered, so a hypothesis that declines to guess a category is worth more than
    one that picks the nearest — an unscored miss is honest, a wrong code is a false positive.
    """

    title: str
    description: str = ""
    category: str | None = None
    score: float = 0.0
    rank: int = 0
    selected: bool = False
    supporting_evidence: list[int] = field(default_factory=list)


@dataclass(slots=True)
class InvestigationContext:
    """Everything one run of the agent knows.

    Mutable, and shared: the nodes write into the same object rather than returning copies,
    because the runner emits an event after every transition and those events have to describe
    the state the investigation is actually in.
    """

    investigation_id: str
    incident_code: str
    query: str
    service_hint: str | None = None

    # Filled in by PLAN. None until then, which is why every reader of it has to cope with None
    # — a run that fails during routing still gets a timeline.
    route: RouteDecision | None = None

    evidence: list[EvidenceItem] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)

    tool_budget: int = DEFAULT_TOOL_BUDGET
    tool_calls_made: int = 0

    iteration: int = 0
    max_iterations: int = DEFAULT_MAX_ITERATIONS

    # Free text the nodes leave for each other and for the timeline, e.g. why a collector came
    # back empty. Not evidence: a note is about the investigation, evidence is about the system.
    notes: list[str] = field(default_factory=list)

    @property
    def target_service(self) -> str | None:
        """The service the collectors should point at.

        The route's answer wins over the incident's own service, because the router saw the
        question and the incident row only saw where the alert fired.
        """
        if self.route is not None and self.route.target_service is not None:
            return self.route.target_service

        return self.service_hint

    @property
    def budget_remaining(self) -> int:
        return max(0, self.tool_budget - self.tool_calls_made)

    def spend_tool_call(self, count: int = 1) -> None:
        """Book ``count`` tool calls against the budget, or refuse the whole request.

        All-or-nothing on purpose: a collector that asked for three tools and was given two would
        have to decide which two, and that decision belongs in the collector's own plan rather
        than in whatever happened to be left.
        """
        if count > self.budget_remaining:
            raise ToolBudgetExhaustedError(
                f"{count} tool call(s) requested with {self.budget_remaining} left "
                f"of a budget of {self.tool_budget}"
            )

        self.tool_calls_made += count

    def add_evidence(self, item: EvidenceItem) -> int:
        """Append a fact and return its index, which is how a hypothesis cites it."""
        self.evidence.append(item)

        return len(self.evidence) - 1

    def sources_seen(self) -> set[EvidenceSource]:
        """The distinct sources that produced evidence. Feeds the diversity bonus."""
        return {item.source for item in self.evidence}

    def note(self, message: str) -> None:
        self.notes.append(message)

    def begin_iteration(self) -> bool:
        """Start another pass through the collectors, if there is one left.

        Returns ``False`` when the loop has been round ``max_iterations`` times, which is the
        runner's signal to stop asking for more evidence and reason with what it has.
        """
        if self.iteration >= self.max_iterations:
            return False

        self.iteration += 1

        return True
