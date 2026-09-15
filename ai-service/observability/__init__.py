"""SentinelAI watching itself.

Two halves, kept apart on purpose. :mod:`observability.instruments` is what the code calls and
depends only on the OpenTelemetry API, so it is inert and nearly free when nothing is listening.
:mod:`observability.otel` installs the SDK that makes those calls mean something, and is called
once, from the application's startup.
"""

from observability.instruments import (
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_REFUSED,
    OUTCOME_TIMEOUT,
    OUTCOME_UNAVAILABLE,
    record_investigation,
    record_llm_call,
    record_retrieval,
    record_step,
    record_tool_call,
)
from observability.otel import SERVICE_NAME, configure_telemetry, instrument_app

__all__ = [
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "OUTCOME_REFUSED",
    "OUTCOME_TIMEOUT",
    "OUTCOME_UNAVAILABLE",
    "SERVICE_NAME",
    "configure_telemetry",
    "instrument_app",
    "record_investigation",
    "record_llm_call",
    "record_retrieval",
    "record_step",
    "record_tool_call",
]
