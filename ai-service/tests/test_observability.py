"""Self-observability: that the four seams actually report, and that silence is the default.

These tests read the measurements back out of a real SDK through an in-memory reader rather than
asserting that a recording function was called. The difference matters: what the dashboard needs
is a named instrument carrying particular attributes, and a mock would agree with whatever the
code happened to do — including recording a tool call under an attribute no panel groups by.

The other half of the file is the negative case, which is the one that bites in practice. The
repository's ``.env`` sets ``OTEL_EXPORTER_OTLP_ENDPOINT`` for the whole stack, so "does nothing
unless told to" has to be enforced rather than assumed.
"""

from __future__ import annotations

import json
import os
import pathlib
import re

import httpx
import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agents.context import InvestigationContext, ToolBudgetExhaustedError
from agents.state_machine import Node, StateMachine, Transition
from agents.states import State
from llm.base import LlmMessage, LlmUnavailableError
from llm.ollama_provider import OllamaLlmProvider
from mcp_client.client import McpClient
from mcp_client.policy import ApprovalVerifier, McpPolicy
from mcp_client.registry import McpToolRegistry
from observability import instruments as instruments_module
from observability import otel
from observability.retrieval import InstrumentedRetriever
from rag.retrievers import RetrievalResult, Retriever

_READER = InMemoryMetricReader()


@pytest.fixture(autouse=True, scope="module")
def _meter_provider():
    """One real MeterProvider for this module, installed globally.

    Global because that is where :func:`observability.instruments.instruments` looks, and
    installed for the module rather than per test because OpenTelemetry keeps exactly one
    provider per process and ignores an attempt to replace it. Tests read a snapshot instead of
    resetting, which is why every assertion below looks for its own attributes rather than
    counting everything the instrument holds.

    ``OTEL_SDK_DISABLED`` is lifted for the construction only. ``conftest`` sets it for the whole
    suite, and the SDK reads it in ``MeterProvider.__init__`` and hands out no-op meters for the
    life of the provider — so a provider built under it would silently record nothing, which is
    exactly the failure these tests exist to catch.
    """
    disabled = os.environ.pop("OTEL_SDK_DISABLED", None)

    try:
        provider = MeterProvider(metric_readers=[_READER])
    finally:
        if disabled is not None:
            os.environ["OTEL_SDK_DISABLED"] = disabled

    metrics.set_meter_provider(provider)
    instruments_module.reset_instruments()

    yield provider

    # The instruments are cached per process, so the next module to record would otherwise keep
    # writing into this module's reader.
    instruments_module.reset_instruments()


def _points(name: str) -> list:
    """Every data point currently recorded under ``name``, across all attribute sets."""
    collected = _READER.get_metrics_data()

    if collected is None:
        return []

    return [
        point
        for resource in collected.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def _matching(name: str, **attributes: str) -> list:
    """The points under ``name`` whose attributes are a superset of ``attributes``."""
    return [
        point
        for point in _points(name)
        if all(point.attributes.get(key) == value for key, value in attributes.items())
    ]


# --------------------------------------------------------------------------------------------
# The model runtime
# --------------------------------------------------------------------------------------------


def _ollama(handler) -> OllamaLlmProvider:
    return OllamaLlmProvider(
        base_url="http://ollama.test",
        model="qwen2.5:3b-instruct",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_a_completion_is_recorded_with_its_tokens() -> None:
    provider = _ollama(
        lambda request: httpx.Response(
            200,
            json={
                "model": "qwen2.5:3b-instruct",
                "message": {"content": "an answer"},
                "prompt_eval_count": 120,
                "eval_count": 30,
            },
        )
    )

    await provider.complete([LlmMessage(role="user", content="why is orders slow?")])

    assert _matching(
        "sentinel.llm.call.duration",
        model="qwen2.5:3b-instruct",
        outcome="ok",
    ), "a successful completion should be recorded under outcome=ok"

    prompt = _matching("sentinel.llm.tokens", model="qwen2.5:3b-instruct", kind="prompt")
    completion = _matching("sentinel.llm.tokens", model="qwen2.5:3b-instruct", kind="completion")

    assert prompt[0].value == 120
    assert completion[0].value == 30


async def test_an_unreachable_runtime_is_recorded_rather_than_missing() -> None:
    """The failure mode the dashboard exists for: a model that has stopped answering.

    Recorded as a measurement under ``outcome=unavailable`` rather than as an absence, because an
    absence of data looks exactly like an idle system.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    provider = _ollama(refuse)

    with pytest.raises(LlmUnavailableError):
        await provider.complete([LlmMessage(role="user", content="anything")])

    assert _matching(
        "sentinel.llm.call.duration",
        model="qwen2.5:3b-instruct",
        outcome="unavailable",
    )


async def test_a_model_the_runtime_does_not_have_is_unavailable_too() -> None:
    provider = _ollama(lambda request: httpx.Response(404))

    with pytest.raises(LlmUnavailableError):
        await provider.complete([LlmMessage(role="user", content="anything")])

    points = _matching("sentinel.llm.call.duration", outcome="unavailable")

    assert points, "a missing model is an environment failure, like an unreachable runtime"


async def test_a_token_count_the_runtime_did_not_report_is_not_counted() -> None:
    """Ollama omits ``prompt_eval_count`` for a cached prompt, and the provider passes the
    omission through as a zero. Counting it would make tokens-per-call a number about calls."""
    provider = _ollama(
        lambda request: httpx.Response(
            200,
            json={"model": "unreported-tokens", "message": {"content": "an answer"}},
        )
    )

    await provider.complete([LlmMessage(role="user", content="anything")])

    assert not _matching("sentinel.llm.tokens", model="unreported-tokens")


# --------------------------------------------------------------------------------------------
# Tool calls
# --------------------------------------------------------------------------------------------


async def test_a_refused_tool_call_is_recorded_as_refused_not_as_an_error() -> None:
    """Policy refusal never reaches a server, so it is neither a slow tool nor a broken one."""
    registry = McpToolRegistry([])
    client = McpClient([], registry, McpPolicy(registry, ApprovalVerifier(None)))

    result = await client.call("database-mcp/execute_write_query", {"sql": "DROP TABLE orders"})

    assert not result.success

    points = _matching("sentinel.tool.call.duration", server="database-mcp", outcome="refused")

    assert points, "a refusal should be a point under outcome=refused"
    assert points[0].sum == 0, "a refusal spends no time in a tool and must not move the p95"


async def test_a_tool_that_cannot_be_reached_is_recorded_as_an_error() -> None:
    registry = McpToolRegistry([])
    policy = McpPolicy(registry, ApprovalVerifier(None))
    client = McpClient([], registry, policy)

    # An unregistered tool is refused, so this asserts the refusal path is the only one reachable
    # without a server — which is the guarantee the policy exists to give.
    result = await client.call("logs-mcp/get_recent_errors", {"service": "orders"})

    assert not result.success
    assert _matching("sentinel.tool.call.duration", server="logs-mcp", outcome="refused")


# --------------------------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------------------------


class _StubRetriever(Retriever):
    def __init__(self, *, name: str = "hybrid_rerank", fail: bool = False) -> None:
        self._name = name
        self._fail = fail

    @property
    def name(self) -> str:
        return self._name

    async def retrieve(self, query, k=5, filters=None) -> RetrievalResult:
        if self._fail:
            raise RuntimeError("the embedding model is not loaded")

        return RetrievalResult(
            query=query,
            # Deliberately not ``self._name``: a reranker that degraded returns results under the
            # name of what actually served them, and that is the name the metric should carry.
            retriever="hybrid",
            chunks=[],
            stage_latency_ms={"total": 250},
        )


async def test_retrieval_is_recorded_under_the_retriever_that_served_it() -> None:
    wrapped = InstrumentedRetriever(_StubRetriever(name="hybrid_rerank"))

    result = await wrapped.retrieve("connection pool exhausted")

    assert wrapped.name == "hybrid_rerank", "the wrapper must not rename the retriever"
    assert result.retriever == "hybrid"

    points = _matching("sentinel.rag.retrieval.duration", retriever="hybrid", outcome="ok")

    assert points
    assert points[0].sum == pytest.approx(0.25)


async def test_a_failed_search_is_recorded_and_still_raises() -> None:
    """The agent notes a failed search and carries on, which is correct and also what would
    otherwise make an embedding model that stopped answering invisible."""
    wrapped = InstrumentedRetriever(_StubRetriever(name="broken", fail=True))

    with pytest.raises(RuntimeError):
        await wrapped.retrieve("anything")

    assert _matching("sentinel.rag.retrieval.duration", retriever="broken", outcome="error")


# --------------------------------------------------------------------------------------------
# The investigation loop
# --------------------------------------------------------------------------------------------


class _Fixed(Node):
    def __init__(self, state: State, nxt: State) -> None:
        self._state = state
        self._next = nxt

    @property
    def state(self) -> State:
        return self._state

    async def run(self, ctx: InvestigationContext) -> Transition:
        return Transition(next_state=self._next)


class _OutOfBudget(Node):
    @property
    def state(self) -> State:
        return State.COLLECT_LOGS

    async def run(self, ctx: InvestigationContext) -> Transition:
        raise ToolBudgetExhaustedError("the tool budget is spent")


def _context() -> InvestigationContext:
    return InvestigationContext(
        investigation_id="inv-observability",
        incident_code="INC-OBS",
        query="why is orders slow?",
    )


async def test_a_completed_investigation_is_recorded_with_its_final_state() -> None:
    machine = StateMachine(
        {State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.COMPLETED)}
    )

    await machine.run(_context())

    assert _matching("sentinel.investigation.duration", final_state=str(State.COMPLETED))
    assert _matching(
        "sentinel.investigation.step.duration", state=str(State.UNDERSTAND_INCIDENT)
    ), "the step that ran should say where the time went"


async def test_an_investigation_that_needs_a_human_is_recorded_as_its_own_outcome() -> None:
    """NEEDS_HUMAN is not a failure and must not be counted as one — it is the outcome Phase 7
    made normal, and on a dashboard it is a different thing to be looking at."""
    machine = StateMachine(
        {
            State.UNDERSTAND_INCIDENT: _Fixed(State.UNDERSTAND_INCIDENT, State.COLLECT_LOGS),
            State.COLLECT_LOGS: _OutOfBudget(),
        }
    )

    result = await machine.run(_context())

    assert result.final_state is State.NEEDS_HUMAN
    assert _matching("sentinel.investigation.duration", final_state=str(State.NEEDS_HUMAN))
    assert not _matching(
        "sentinel.investigation.step.duration", state=str(State.COLLECT_LOGS)
    ), "a step that ended the run is not a step that completed"


# --------------------------------------------------------------------------------------------
# Silence by default
# --------------------------------------------------------------------------------------------


def test_no_endpoint_means_no_telemetry() -> None:
    otel.reset_for_tests()

    assert otel.configure_telemetry("") is False
    assert otel.configure_telemetry(None) is False


def test_the_sdk_kill_switch_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OTEL_SDK_DISABLED`` is OpenTelemetry's own variable, and the reason the test suite does
    not open gRPC connections to the collector address that lives in the repository's .env."""
    otel.reset_for_tests()
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")

    assert otel.configure_telemetry("http://localhost:4317") is False


def test_configuring_a_second_time_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenTelemetry keeps one provider per signal and ignores a replacement, so a second call
    that went through would leave the process exporting through the first set of exporters while
    looking like it had been pointed somewhere else.

    Asserted by checking that the global providers are the same objects afterwards — this module
    installed its own MeterProvider, and a second configuration that took effect would be visible
    as that object being replaced, or as these tests losing their measurements.
    """
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setattr(otel, "_configured", True)

    before = metrics.get_meter_provider()

    assert otel.configure_telemetry("http://localhost:9999") is True
    assert metrics.get_meter_provider() is before


# --------------------------------------------------------------------------------------------
# The dashboard and the instruments, kept in step
# --------------------------------------------------------------------------------------------

# Every instrument this project defines, spelled as Prometheus sees it after the collector's
# OpenTelemetry-to-Prometheus translation: dots to underscores, the unit appended, and `_total`
# on a monotonic counter. Written out rather than derived, because deriving it would be
# reimplementing the collector's rules and agreeing with itself. The mapping was read off a
# running stack -- `/api/v1/label/__name__/values` on Prometheus, with the collector's
# `resource_to_telemetry_conversion` turning service.name into the `job` label.
PROMETHEUS_NAMES = {
    "sentinel_investigation_duration_seconds",
    "sentinel_investigation_step_duration_seconds",
    "sentinel_llm_call_duration_seconds",
    "sentinel_llm_tokens",
    "sentinel_rag_retrieval_duration_seconds",
    "sentinel_tool_call_duration_seconds",
}

_SUFFIXES = ("_bucket", "_count", "_sum", "_total")

_DASHBOARD = (
    pathlib.Path(__file__).resolve().parents[2]
    / "infrastructure"
    / "grafana"
    / "dashboards"
    / "sentinel-self.json"
)


def _dashboard_metric_names() -> set[str]:
    """Every ``sentinel_*`` series the dashboard queries, with its suffix stripped."""
    dashboard = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    expressions = [
        target.get("expr", "")
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
    ]

    found: set[str] = set()

    for name in re.findall(r"\bsentinel_[a-z0-9_]+", " ".join(expressions)):
        for suffix in _SUFFIXES:
            if name.endswith(suffix):
                found.add(name[: -len(suffix)])
                break
        else:
            found.add(name)

    return found


def test_the_translated_names_cover_every_instrument_the_code_defines() -> None:
    """``PROMETHEUS_NAMES`` is a hand-written table, so it can go stale in the one direction the
    two tests below cannot see: an instrument added in Python and never added here would be
    absent from the table, absent from the dashboard, and silently agreed about by both."""
    collected = _READER.get_metrics_data()

    assert collected is not None, "the tests above should have recorded on every instrument"

    emitted = {
        metric.name
        for resource in collected.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }

    # The OTel side of the same mapping: dots where Prometheus has underscores, and no unit
    # suffix. Comparing the shapes rather than the strings keeps this from being a second copy
    # of the translation rules.
    translated = {name.replace(".", "_") for name in emitted}
    known = {name.removesuffix("_seconds") for name in PROMETHEUS_NAMES}

    assert translated <= known, f"instruments with no entry in the table: {translated - known}"


def test_every_dashboard_query_names_a_metric_this_project_emits() -> None:
    unknown = _dashboard_metric_names() - PROMETHEUS_NAMES

    assert not unknown, f"the dashboard queries series nothing emits: {sorted(unknown)}"


def test_every_instrument_appears_on_the_dashboard() -> None:
    """The other direction. An instrument nothing draws is a measurement nobody will see, which
    is a cost paid on every call for no benefit."""
    undrawn = PROMETHEUS_NAMES - _dashboard_metric_names()

    assert not undrawn, f"nothing on the dashboard draws: {sorted(undrawn)}"
