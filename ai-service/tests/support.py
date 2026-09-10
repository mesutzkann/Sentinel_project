"""Fakes the agent tests share: a model that says what the test wrote, and a context to run in.

Not a fixture module by accident — the reasoning nodes are tested against a scripted provider
rather than a running Ollama on purpose. What is being checked is what the node does with an
answer: which transition it takes, what it puts on the context, what it refuses to keep. Those
are properties of the node, and a test that called a 3B model to check them would be measuring
the model's mood on the day.

The model's *quality* is measured too, but by the agent benchmark against real scenarios in
Phase 11, which is the place where a real model belongs.
"""

from __future__ import annotations

from agents.context import EvidenceItem, EvidenceSource, InvestigationContext
from llm.base import LlmCompletion, LlmMessage, LlmOptions, LocalLlmProvider


class ScriptedProvider(LocalLlmProvider):
    """Returns queued responses in order and records what it was asked.

    Deliberately raises rather than repeating its last answer when it runs out: a node that
    called the model more times than the test expected has changed behaviour, and a provider that
    quietly kept answering would hide it.
    """

    def __init__(self, responses: list[str], *, model: str = "scripted") -> None:
        self._responses = list(responses)
        self._model = model
        self.conversations: list[list[LlmMessage]] = []
        self.options: list[LlmOptions] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def calls(self) -> int:
        return len(self.conversations)

    @property
    def last_prompt(self) -> str:
        return self.conversations[-1][0].content

    async def complete(
        self,
        messages: list[LlmMessage],
        options: LlmOptions | None = None,
    ) -> LlmCompletion:
        self.conversations.append(list(messages))
        self.options.append(options or LlmOptions())

        if not self._responses:
            raise AssertionError("the provider was called more times than the test scripted")

        return LlmCompletion(
            text=self._responses.pop(0),
            model=self._model,
            prompt_tokens=100,
            completion_tokens=20,
            latency_ms=5,
        )

    async def is_available(self) -> bool:
        return True


def context(**overrides: object) -> InvestigationContext:
    """An investigation of the demo scenario, which is what most of these tests are about."""
    defaults: dict[str, object] = {
        "investigation_id": "11111111-1111-1111-1111-111111111111",
        "incident_code": "INC-00142",
        "query": "orders is timing out, find out why",
        "service_hint": "orders",
    }

    return InvestigationContext(**{**defaults, **overrides})  # type: ignore[arg-type]


def pool_evidence() -> list[EvidenceItem]:
    """Three facts of the shape the collectors actually produce for the pool scenario.

    Two sources rather than one so that the diversity half of evidence support has something to
    measure, and one negative finding so that "it looked and found nothing" is represented — that
    is the fact the collectors weight at 0.4 and the one the critic is meant to notice.
    """
    return [
        EvidenceItem(
            source=EvidenceSource.DATABASE,
            summary="database connections: 200 of 200 used, 0 spare",
            weight=0.9,
            raw={"used": 200, "max_connections": 200, "headroom": 0},
            tool="database-mcp/get_connection_count",
        ),
        EvidenceItem(
            source=EvidenceSource.LOGS,
            summary="recent errors: 347 in the last 30 minutes — timeout acquiring connection",
            weight=0.8,
            raw={"found": 347},
            tool="logs-mcp/get_recent_errors",
        ),
        EvidenceItem(
            source=EvidenceSource.DATABASE,
            summary="deadlocks since reset: 0; sessions blocked now: 0",
            weight=0.4,
            raw={"deadlocks_since_reset": 0},
            tool="database-mcp/get_locks_and_deadlocks",
        ),
    ]
