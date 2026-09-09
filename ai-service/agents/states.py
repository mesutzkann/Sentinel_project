"""The states, in their own module because two things need them and neither should own them.

``context.py`` holds the plan the PLAN node builds, which is a list of states; ``state_machine.py``
runs them. Putting the enum in either one makes the other import it and closes a cycle, so it
lives here and both depend on it.

The list is the one in docs/planning.md §7, unchanged. It is written down rather than derived
because Phase 11 compares agent runs against each other and a state name is what a step is keyed
by: renaming one after evaluation data exists silently breaks the comparison.
"""

from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    """Where an investigation is.

    The order of declaration is the order of the happy path, which is also the order the
    frontend lays the graph out in. It is not the order they always execute in: PLAN skips the
    collectors the question does not need, and COLLECT_ADDITIONAL_EVIDENCE goes backwards.
    """

    UNDERSTAND_INCIDENT = "UNDERSTAND_INCIDENT"
    PLAN = "PLAN"

    COLLECT_LOGS = "COLLECT_LOGS"
    COLLECT_METRICS = "COLLECT_METRICS"
    COLLECT_TRACES = "COLLECT_TRACES"

    # Not in the planning document's list, and it should have been. That list has no state that
    # calls database-mcp, while the evidence enum it also specifies has a `database` source and
    # the demo scenario's own expected-tools row is `get_recent_errors, get_connection_count,
    # get_response_time`. Connection counts, lock pairs and slow statements are the discriminator
    # for four of the fifteen scenarios; folding them into COLLECT_METRICS would have made a
    # different claim — that the database is a metric — and made the timeline say so.
    COLLECT_DATABASE = "COLLECT_DATABASE"

    CHECK_DEPLOYMENTS = "CHECK_DEPLOYMENTS"
    SEARCH_HISTORY = "SEARCH_HISTORY"
    INSPECT_CODE = "INSPECT_CODE"

    GENERATE_HYPOTHESES = "GENERATE_HYPOTHESES"
    COLLECT_ADDITIONAL_EVIDENCE = "COLLECT_ADDITIONAL_EVIDENCE"
    RANK_HYPOTHESES = "RANK_HYPOTHESES"
    SELECT_ROOT_CAUSE = "SELECT_ROOT_CAUSE"
    VALIDATE = "VALIDATE"
    RECOMMEND_FIX = "RECOMMEND_FIX"

    # Terminal. Three of them rather than one because they mean different things to a human:
    # COMPLETED has a conclusion, NEEDS_HUMAN has evidence and no conclusion it will stand
    # behind, and FAILED has a defect. Collapsing the middle one into either would either
    # overstate a result or throw away work that is worth looking at.
    COMPLETED = "COMPLETED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    FAILED = "FAILED"


TERMINAL_STATES = frozenset({State.COMPLETED, State.NEEDS_HUMAN, State.FAILED})

# The states that call tools and add evidence, in the order PLAN considers them. SEARCH_HISTORY
# is among them and is the odd one: it reads the knowledge base rather than the running system,
# so it costs no tool budget and is the one collector that still works with every MCP server
# down.
COLLECTOR_STATES = (
    State.COLLECT_LOGS,
    State.COLLECT_METRICS,
    State.COLLECT_TRACES,
    State.COLLECT_DATABASE,
    State.CHECK_DEPLOYMENTS,
    State.SEARCH_HISTORY,
    State.INSPECT_CODE,
)
