"""What SentinelAI measures about itself.

The project's own answer to its own question. SentinelAI investigates services through Loki,
Prometheus and Jaeger; the four things it spends its time on — model calls, tool calls,
retrieval and the investigation loop itself — are reported into the same three, so the dashboard
that watches the sample services and the dashboard that watches the agent are built out of the
same queries.

Only the OpenTelemetry *API* is imported here, never the SDK. Without a configured
``MeterProvider`` every instrument below is a no-op object, which is what makes it safe to record
from the middle of an investigation in a test run, in an evaluation, or on a machine with no
collector: the call costs an attribute lookup and returns.

The recorded shape is deliberate: **one histogram per seam, carrying an ``outcome`` attribute,
rather than a counter for calls and a second counter for failures.** A histogram already carries
its own count, so ``rate(..._count[5m])`` is the call rate and the same series split by
``outcome`` is the error rate — two numbers that cannot disagree about how many calls there were.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from opentelemetry import metrics

logger = logging.getLogger(__name__)

# The name the meter reports under. Distinct from the service name, so a panel can tell a
# measurement this project took about itself from one an instrumentation library took for it.
INSTRUMENTATION_NAME: Final = "sentinel.ai-service"

# Outcomes, spelled once. They are attribute values, which means a typo is not an error — it is
# a second series that looks like another kind of outcome.
OUTCOME_OK: Final = "ok"
OUTCOME_ERROR: Final = "error"
OUTCOME_UNAVAILABLE: Final = "unavailable"
OUTCOME_REFUSED: Final = "refused"
OUTCOME_TIMEOUT: Final = "timeout"

# Bucket boundaries, per seam, because the defaults fit none of them. OTel's default histogram
# tops out at 10 s: against a 7B model that averaged 43 s per call under contention, every model
# call would land in the +Inf bucket and `histogram_quantile` would report the ceiling for p50,
# p95 and p99 alike. These are read off numbers this project has actually measured — 74-140 s
# investigations, a 2.3 s router p95, sub-second retrieval — with room above the slowest observed
# value, so that a regression has somewhere to show up.
_LLM_BUCKETS: Final = (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 45.0, 60.0, 90.0, 120.0, 300.0)
_TOOL_BUCKETS: Final = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_RETRIEVAL_BUCKETS: Final = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0)
_INVESTIGATION_BUCKETS: Final = (5.0, 10.0, 20.0, 30.0, 60.0, 90.0, 120.0, 180.0, 300.0, 600.0)
_STEP_BUCKETS: Final = (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0)


class _Instruments:
    """The instrument set, built once on first use.

    Lazily rather than at import, because this module is imported long before
    :func:`observability.otel.configure_telemetry` has had a chance to install a provider, and an
    instrument created against the no-op provider stays a no-op for the life of the process.
    """

    def __init__(self) -> None:
        meter = metrics.get_meter(INSTRUMENTATION_NAME)

        self.llm_duration = meter.create_histogram(
            "sentinel.llm.call.duration",
            unit="s",
            description="Wall clock of one call to the local model runtime.",
            explicit_bucket_boundaries_advisory=list(_LLM_BUCKETS),
        )
        self.llm_tokens = meter.create_counter(
            "sentinel.llm.tokens",
            unit="{token}",
            description="Tokens the runtime reported, prompt and completion counted apart.",
        )
        self.tool_duration = meter.create_histogram(
            "sentinel.tool.call.duration",
            unit="s",
            description="Wall clock of one MCP tool call, policy included.",
            explicit_bucket_boundaries_advisory=list(_TOOL_BUCKETS),
        )
        self.retrieval_duration = meter.create_histogram(
            "sentinel.rag.retrieval.duration",
            unit="s",
            description="Wall clock of one knowledge-base search.",
            explicit_bucket_boundaries_advisory=list(_RETRIEVAL_BUCKETS),
        )
        self.investigation_duration = meter.create_histogram(
            "sentinel.investigation.duration",
            unit="s",
            description="Wall clock of one investigation, first state to terminal state.",
            explicit_bucket_boundaries_advisory=list(_INVESTIGATION_BUCKETS),
        )
        self.step_duration = meter.create_histogram(
            "sentinel.investigation.step.duration",
            unit="s",
            description="Wall clock of one state machine step.",
            explicit_bucket_boundaries_advisory=list(_STEP_BUCKETS),
        )


_instruments: _Instruments | None = None


def instruments() -> _Instruments:
    """The process's instruments, built on the first call."""
    global _instruments

    if _instruments is None:
        _instruments = _Instruments()

    return _instruments


def reset_instruments() -> None:
    """Drop the cached set, so the next call binds against the current provider.

    For tests, and for :func:`observability.otel.configure_telemetry`, which installs a provider
    after this module may already have been imported and used.
    """
    global _instruments

    _instruments = None


def _record(instrument: Any, value: float, attributes: dict[str, Any]) -> None:
    """Record one measurement, and never be the reason the work around it failed.

    Recording is already cheap and non-throwing in both the API and the SDK, so this guard is for
    the case that is not: an instrument bound to a provider that has since been shut down. Logged
    at warning rather than swallowed — telemetry that has stopped working should be visible in
    the logs, which are the other signal.
    """
    try:
        instrument.record(value, attributes)
    except Exception as exc:  # noqa: BLE001 - a metric must not fail the work it measures
        logger.warning("dropping a measurement for %s: %s", attributes, exc)


def record_llm_call(
    *,
    model: str,
    duration_ms: int,
    outcome: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """One call to the model runtime, whether or not it produced a completion.

    Token counts are reported only when the runtime reported them. Ollama omits
    ``prompt_eval_count`` on some versions and for a cached prompt, and the provider passes that
    through as a zero meaning "not reported". Adding a zero would add nothing to the counter, but
    it would make tokens-per-call a number about calls rather than about tokens.
    """
    _record(instruments().llm_duration, duration_ms / 1000, {"model": model, "outcome": outcome})

    if prompt_tokens:
        instruments().llm_tokens.add(prompt_tokens, {"model": model, "kind": "prompt"})

    if completion_tokens:
        instruments().llm_tokens.add(completion_tokens, {"model": model, "kind": "completion"})


def record_tool_call(*, server: str, tool: str, duration_ms: int, outcome: str) -> None:
    """One MCP tool call.

    ``server`` and ``tool`` are separate attributes rather than the qualified name, because the
    question asked on a dashboard is usually about a server — whether database-mcp is slow — and
    a qualified name would have to be taken apart in PromQL to answer it.
    """
    _record(
        instruments().tool_duration,
        duration_ms / 1000,
        {"server": server, "tool": tool, "outcome": outcome},
    )


def record_retrieval(*, retriever: str, duration_ms: int, outcome: str = OUTCOME_OK) -> None:
    """One knowledge-base search, labelled with the retriever that served it.

    A search that raised is recorded too. The agent treats a failed search as a note and carries
    on — correctly, since history is supporting evidence rather than the investigation — which
    means an embedding model that has stopped answering costs the agent quality without costing
    it an error anywhere. This series is where that shows.
    """
    _record(
        instruments().retrieval_duration,
        duration_ms / 1000,
        {"retriever": retriever, "outcome": outcome},
    )


def record_investigation(*, final_state: str, duration_ms: int) -> None:
    """One finished investigation.

    ``final_state`` is the attribute that matters. COMPLETED, NEEDS_HUMAN and FAILED are three
    different things to be looking at on a dashboard, and Phase 7 made the middle one a normal
    outcome rather than a failure.
    """
    _record(
        instruments().investigation_duration,
        duration_ms / 1000,
        {"final_state": final_state},
    )


def record_step(*, state: str, duration_ms: int) -> None:
    """One state machine step, labelled with the state that ran.

    This is the series that says where an investigation's minutes went, which the total on its
    own cannot.
    """
    _record(instruments().step_duration, duration_ms / 1000, {"state": state})
