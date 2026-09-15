"""Test-wide setup that has to happen before anything imports the application.

One thing so far, and it has to be here rather than in a fixture: the repository's ``.env`` sets
``OTEL_EXPORTER_OTLP_ENDPOINT`` for the whole stack, and ``app.main`` reads it at import time.
Without this, importing the application from a test installs real OTLP exporters and leaves
batch-export threads retrying into a collector that is not running — which is slow, noisy, and
makes the suite depend on whether the developer happens to have compose up.

``OTEL_SDK_DISABLED`` is OpenTelemetry's own variable, not one invented here, so setting it turns
off every SDK in the process rather than only the parts this project configures.
"""

from __future__ import annotations

import os

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
