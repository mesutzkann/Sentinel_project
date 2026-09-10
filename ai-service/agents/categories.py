"""The fifteen things an investigation can conclude, and the discipline about not guessing.

These are the chaos scenario codes from ``sample-services/chaos/scenarios.md``, mirrored from
``ChaosCodes.cs``. They are a cross-cutting contract: the samples trigger them, ``root_causes.
category`` stores them, and Phase 11 compares what the agent concluded against the scenario that
was actually enabled. A code renamed here after evaluation data exists silently breaks that
comparison, which is why the list is written out rather than derived from anything.

**Why an unknown category becomes ``None`` instead of a validation failure.** The model is asked
for a code and a 3B model will sometimes answer ``DB_POOL_EXHAUSTED`` or ``database pool``. Two
options: reject and make it try again, or take the near miss as a miss. Rejecting spends a repair
round trip on a label, and the label is not what the answer is — the explanation and the evidence
are. Taking the near miss costs nothing and keeps the agent's own scoring honest: an uncategorised
root cause scores as a miss in Phase 11, a wrongly categorised one scores as a false positive, and
of the two the miss is the one that does not claim something untrue.

``UNKNOWN`` exists only at the boundary. The backend's ``RootCause.Category`` is a required
string, so a conclusion the agent would not name has to cross the callback as *something*; it
crosses as the word for "not named" rather than as the nearest code.
"""

from __future__ import annotations

from enum import StrEnum

# What a category is called when the agent would not name one. Not a member of the enum: it is
# not a scenario, it is the absence of one, and putting it in the enum would let it be selected.
UNKNOWN_CATEGORY = "UNKNOWN"


class ScenarioCategory(StrEnum):
    """The fifteen scenario codes, grouped as ``scenarios.md`` groups them."""

    # A. Database
    DB_CONNECTION_POOL_EXHAUSTION = "DB_CONNECTION_POOL_EXHAUSTION"
    DB_SLOW_QUERY_MISSING_INDEX = "DB_SLOW_QUERY_MISSING_INDEX"
    DB_DEADLOCK = "DB_DEADLOCK"
    DB_N_PLUS_ONE_QUERY = "DB_N_PLUS_ONE_QUERY"

    # B. Code defects
    NULL_REFERENCE_EXCEPTION = "NULL_REFERENCE_EXCEPTION"
    DIVIDE_BY_ZERO_EDGE_CASE = "DIVIDE_BY_ZERO_EDGE_CASE"
    MEMORY_LEAK = "MEMORY_LEAK"

    # C. Configuration
    TIMEOUT_TOO_LOW = "TIMEOUT_TOO_LOW"
    WRONG_CONNECTION_STRING = "WRONG_CONNECTION_STRING"
    RETRY_STORM = "RETRY_STORM"

    # D. Dependency and cascade
    DOWNSTREAM_LATENCY_CASCADE = "DOWNSTREAM_LATENCY_CASCADE"
    EXTERNAL_DEPENDENCY_UNAVAILABLE = "EXTERNAL_DEPENDENCY_UNAVAILABLE"
    CIRCUIT_BREAKER_STUCK_OPEN = "CIRCUIT_BREAKER_STUCK_OPEN"

    # E. Deployment and resources
    BAD_DEPLOYMENT_REGRESSION = "BAD_DEPLOYMENT_REGRESSION"
    CPU_SATURATION = "CPU_SATURATION"


def normalise_category(value: str | None) -> str | None:
    """A scenario code, or ``None`` for anything that is not exactly one of them.

    Case and separators are forgiven because they are transcription, not meaning:
    ``db-deadlock`` and ``DB_DEADLOCK`` are the same claim typed differently. Nothing else is —
    a code that is merely close to a real one is a different scenario, and the agent gets no
    credit for being nearly right about which failure this was.
    """
    if value is None:
        return None

    candidate = value.strip().upper().replace("-", "_").replace(" ", "_")

    if candidate in {"", UNKNOWN_CATEGORY, "NONE", "NULL"}:
        return None

    try:
        return ScenarioCategory(candidate).value
    except ValueError:
        return None


def category_or_unknown(value: str | None) -> str:
    """The value to put on the wire, where the backend requires one."""
    return value or UNKNOWN_CATEGORY


#: Rendered into the prompts that ask for a category. One line rather than the enum's repr so
#: that the model sees the vocabulary it is being asked to choose from and nothing else.
CATEGORY_LIST = ", ".join(member.value for member in ScenarioCategory)
