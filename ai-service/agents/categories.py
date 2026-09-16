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

**Making the model name one was tried, measured, and reverted.** Most hypotheses arrive with a
null category — four of five on the pool scenario, three of four on the null-reference one, where
the model titled a hypothesis "NullReferenceException due to..." and left the field empty beside
it. Nothing is being discarded on the way: it writes ``null`` itself. Two changes were built to
stop it — ``category`` typed as the enum, so the fifteen codes are in the grammar rather than in
prose, and a ``hypotheses.v2`` that asked for "the code this hypothesis *is*". Category coverage
went from a quarter to over four fifths, and the null-reference scenario began proposing
``NULL_REFERENCE_EXCEPTION`` where it never had.

Root cause accuracy fell from three of five to two, twice, and both wrong conclusions now *passed*
the confidence threshold — an agent that finishes confidently on the wrong diagnosis, which is the
one outcome this phase exists to prevent. A 3B asked to put a code on every hypothesis spreads
them across the vague ones too, and the conclusion inherits whichever won. The nulls were
accidentally protective: the model named a code for the hypothesis it was sure of, and that one
was usually right. The enum alone, with the old prompt, measured the same as changing nothing.

So the vocabulary stays in prose and the nulls stay. The cost is real and known: a hypothesis with
no category cannot be matched against ``CATEGORY_SIGNALS``, so the back-loop in
``COLLECT_ADDITIONAL_EVIDENCE`` almost never fires — every measured run notes "every hypothesis
rests on a signal that was collected". Worth revisiting with a bigger model, or by asking for the
category once, for the conclusion, rather than five times for the candidates.
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


# What tells each code apart from the ones it is confused with. The fifteen lines are the
# ``Discriminator`` rows of ``sample-services/chaos/scenarios.md``, which is where they were
# written and measured; scenario numbers are spelled as codes here because the model is never
# shown the catalogue, and the one line that described what the benchmark was testing rather
# than what the failure looks like is restated as the signal.
#
# **Why the codes needed this at all.** The conclusion prompt listed fifteen names and no
# definitions, and the 15-case run on 2026-09-16 put the right cause in prose beside the wrong
# code five times out of nine — "NullReferenceException due to Currency Code Mapping" labelled
# BAD_DEPLOYMENT_REGRESSION, "Delivery Provider is Unavailable" labelled
# DOWNSTREAM_LATENCY_CASCADE. Three codes took seven of the nine misses, and they are the three
# that are loosely true of almost any incident: a defect did ship in a deployment, a slow query
# could be missing an index, and there is always something downstream. Against a vague code that
# is never quite wrong, a specific one that is exactly right loses.
#
# **Why all fifteen rather than only the confused pairs.** Four hand-written rules for the
# measured confusions scored 0.40 → 0.47, but cost R14: describing DOWNSTREAM_LATENCY_CASCADE's
# proper scope made it more attractive, and a case that had been right moved onto it. Defining
# some boundaries and not others moves every case that borders an undefended one. The catalogue
# defines all fifteen against each other, which is the property that matters.
#
# **This is taxonomy, not answers.** Each line says what distinguishes a code from its
# neighbours — the same kind of statement the runbooks in the corpus already make, and the
# reason `datasets/knowledge/runbooks/` exists. It does not say which one this incident is:
# the model still has to match a signature to the evidence, and the same run shows it failing
# to even when told, concluding a 503 from the delivery provider with no category at all.
#
# Deliberately not applied to hypotheses. Pushing categories there was measured and reverted (see
# this module's docstring): five vague candidates spread the codes and the conclusion inherited
# whichever won. This is one call, on one conclusion that has already been argued.
CATEGORY_DISCRIMINATORS = """\
- DB_CONNECTION_POOL_EXHAUSTION: latency is high and the errors are timeouts, where
  CIRCUIT_BREAKER_STUCK_OPEN fails instantly.
- DB_SLOW_QUERY_MISSING_INDEX: slow without errors, and the slowness sits inside a single
  database span.
- DB_DEADLOCK: errors are intermittent and self-recovering, never sustained.
- DB_N_PLUS_ONE_QUERY: many fast queries, where DB_SLOW_QUERY_MISSING_INDEX is one slow one.
- NULL_REFERENCE_EXCEPTION: fast failures on a subset of requests, latency unaffected.
- DIVIDE_BY_ZERO_EDGE_CASE: rare errors correlated with a specific input rather than with load
  or time — the same input fails every time and every other input succeeds.
- MEMORY_LEAK: the only one where a metric rises monotonically over time rather than stepping,
  spiking or collapsing.
- TIMEOUT_TOO_LOW: the caller fails while the callee succeeds — look upstream, at the deadline.
- WRONG_CONNECTION_STRING: 100% failure of one service while the database is provably healthy,
  and no other service is affected.
- RETRY_STORM: downstream load rises while upstream load does not — amplification.
- DOWNSTREAM_LATENCY_CASCADE: a multi-service symptom with a single-service cause. Follow the
  trace to the slow span rather than blaming the loudest service.
- EXTERNAL_DEPENDENCY_UNAVAILABLE: the failing span is outside the service boundary.
- CIRCUIT_BREAKER_STUCK_OPEN: fast failures. Low latency together with a high error rate is
  unique to this one and separates it from DB_CONNECTION_POOL_EXHAUSTION and
  DOWNSTREAM_LATENCY_CASCADE.
- BAD_DEPLOYMENT_REGRESSION: temporal correlation is the signal — onset aligns with a
  deployment, not with load, input or elapsed time.
- CPU_SATURATION: slow with no database or network involvement — pure compute.\
"""
