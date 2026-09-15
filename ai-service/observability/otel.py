"""Installing the OpenTelemetry SDK, when there is somewhere to send the signals.

The same decision the backend makes in ``TelemetryExtensions.cs``, for the same reason: silent
when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset, so the AI service still runs against a bare
``docker compose --profile core up`` — or against nothing at all — with no collector to talk to
and no exporter retrying into a closed port. Console logging is unaffected either way; it is the
sink a developer actually watches.

The SDK is imported inside :func:`configure_telemetry` rather than at module scope. That keeps
the import cost off every process that imports this package for its instruments — the evaluation
runners, the tests — and it means a missing SDK degrades to a warning rather than to an
ImportError on startup.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from observability.instruments import reset_instruments

logger = logging.getLogger(__name__)

# The string every cross-signal query joins on. It is ``service.name`` on the resource, which the
# collector promotes to the `job` label on metrics and the `service_name` label on logs, so this
# one value is what makes a metric panel and a log panel about the same process line up.
SERVICE_NAME: Final = "sentinel-ai-service"

# Metrics are pulled from the collector by Prometheus every 10 s. Exporting more often than that
# writes points nothing reads; much less often and a short investigation finishes inside one
# interval, which makes the step-duration series look empty exactly when it is interesting.
_METRIC_EXPORT_INTERVAL_MS: Final = 10_000

# The variable that selects the stable HTTP semantic conventions in opentelemetry-python. Named
# by the specification, not by this project.
_SEMCONV_OPT_IN: Final = "OTEL_SEMCONV_STABILITY_OPT_IN"

_configured = False


def _sdk_disabled() -> bool:
    """Whether ``OTEL_SDK_DISABLED`` is set to true, per the OpenTelemetry specification."""
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


def configure_telemetry(endpoint: str | None, *, service_name: str = SERVICE_NAME) -> bool:
    """Send traces, metrics and logs to ``endpoint``, if one is configured.

    Returns whether telemetry was installed, so the caller can log which of the two it got rather
    than leaving "is this thing reporting?" to be answered by looking for data.

    Idempotent. A second call is a no-op with a warning: OpenTelemetry keeps one global provider
    per signal and quietly ignores an attempt to replace it, so a double-configure would leave
    the process exporting through the first set of exporters while looking like it had been
    reconfigured.
    """
    global _configured

    if not endpoint:
        return False

    if _sdk_disabled():
        # OpenTelemetry's own kill switch, honoured here because the endpoint lives in the `.env`
        # the whole stack shares: without it, anything that imports the application picks up a
        # developer's collector address and starts a batch exporter retrying into it. That is how
        # the test suite came to open gRPC connections to a port nothing was listening on.
        logger.info("telemetry: disabled by OTEL_SDK_DISABLED")
        return False

    if _configured:
        logger.warning("telemetry is already configured; ignoring the endpoint %s", endpoint)
        return True

    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        # Reachable when the service is installed without its SDK dependencies. A warning rather
        # than a failure: an AI service that will not start because it cannot report on itself is
        # a worse outcome than one that runs unobserved.
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but the SDK is missing: %s", exc)
        return False

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": "local",
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(tracer_provider)

    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint),
                    export_interval_millis=_METRIC_EXPORT_INTERVAL_MS,
                )
            ],
        )
    )

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
    )
    set_logger_provider(logger_provider)

    # Attached to the root logger, so every module's ``logging.getLogger(__name__)`` reaches Loki
    # without any of them knowing about OpenTelemetry. The console handler installed by
    # ``basicConfig`` stays where it is; this is an additional sink, not a replacement.
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))

    # Any instrument built before this point is bound to the no-op provider and would stay a
    # no-op forever. Dropping the cache makes the next record rebuild against the real one.
    reset_instruments()

    _configured = True
    logger.info("telemetry: reporting to %s as %s", endpoint, service_name)

    return True


def instrument_app(app: object) -> None:
    """Add ASGI and HTTP-client instrumentation, if the packages are installed.

    Kept apart from :func:`configure_telemetry` because the two answer different questions.
    That one is about where signals go; this is about who produces them, and the FastAPI
    middleware has to be added to the application object before it starts serving.

    The point of instrumenting the ASGI side is that it emits the same
    ``http.server.request.duration`` the .NET services do, under the same semantic conventions.
    The AI service therefore appears in the existing Service Health dashboard by itself, and the
    self-observability dashboard is left to cover only what is specific to this project.
    """
    # Not optional, and not a preference. opentelemetry-python still defaults to the pre-1.21
    # HTTP conventions, so without this the service reports `http.server.duration` in
    # milliseconds while the .NET services report `http.server.request.duration` in seconds —
    # two names for one measurement, and an AI service that is invisible on every panel that
    # queries the second. Set before the instrumentation is imported, because the stability mode
    # is read once and cached the first time an instrumentation asks for it.
    os.environ.setdefault(_SEMCONV_OPT_IN, "http")

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    except ImportError as exc:
        logger.warning("HTTP instrumentation is unavailable: %s", exc)
        return

    # Health is excluded for the same reason the backend excludes it: a liveness probe every few
    # seconds is the loudest endpoint in any latency histogram and says nothing about the work.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health")

    # httpx is how this service reaches Ollama, the backend and every MCP server, so client spans
    # are what make a slow investigation attributable to a dependency rather than to the agent.
    HTTPXClientInstrumentor().instrument()


def reset_for_tests() -> None:
    """Forget that telemetry was configured. Test-only."""
    global _configured

    _configured = False
    reset_instruments()
