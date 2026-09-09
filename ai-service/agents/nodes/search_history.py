"""The one collector that reads what the organisation already knew.

It is the odd collector in three ways, and all three are deliberate.

*It spends no tool budget.* The budget bounds calls to the observability stack, which are what
make an investigation slow and what fill a prompt. This reads the knowledge base — this service's
own PostgreSQL — so it costs one embedding call and a fused search, and bounding it against the
same twenty-five would mean an investigation could run out of budget before it ever asked whether
this has happened before.

*It works when everything else is down.* No MCP server is involved, so a stack with Loki, Jaeger
and Prometheus all unreachable still answers "have we seen this before". That is the failure mode
worth being good at: an outage is when the observability stack is least likely to be healthy.

*Its evidence is weighted below anything measured.* A runbook describing a symptom is not the
same kind of fact as observing it, and the confidence score must not be able to reach a
recommendation from documents alone. ``WEIGHT_DOCUMENT`` caps this collector below the weight the
live collectors assign to a positive finding, so retrieval can corroborate a conclusion and
cannot by itself justify one.

It retrieves with ``hybrid_rerank``. The Phase 6 benchmark is why: over 120 queries it puts the
right document in the top three for 0.956 of them against fusion's 0.910, and it costs about a
second. In an investigation that will spend minutes on generation, a second buying that is not a
trade worth thinking about twice.
"""

from __future__ import annotations

import logging

from agents.context import EvidenceItem, EvidenceSource, InvestigationContext
from agents.state_machine import AgentEvent, EventType, Node, Transition
from agents.states import State
from rag.documents import RetrievedChunk, SourceType
from rag.retrievers import Retriever

logger = logging.getLogger(__name__)

# The ceiling on retrieved evidence. Below WEIGHT_POSITIVE (0.8) so that no amount of relevant
# documentation outweighs what the system was actually observed doing.
WEIGHT_DOCUMENT = 0.5

# Second and later documents. A ranked list is a claim about relevance, and flattening it would
# throw away the only signal retrieval gives about its own confidence.
WEIGHT_DOCUMENT_TAIL = 0.35

# Documents to carry into evidence. Five chunks come back; they usually cover two or three
# documents, and an investigation wants the runbook and the postmortem rather than four chunks of
# one runbook.
DEFAULT_DOCUMENTS = 3

# Past incidents and postmortems are a different kind of fact from a runbook: one says this
# happened here before, the other says this is how the failure works. The confidence score's
# diversity bonus counts them separately because they corroborate independently.
_HISTORICAL = frozenset({SourceType.INCIDENT, SourceType.POSTMORTEM})


class SearchHistoryNode(Node):
    """Searches the knowledge base for what is already known about this failure."""

    def __init__(
        self,
        retriever: Retriever,
        *,
        k: int = 5,
        documents: int = DEFAULT_DOCUMENTS,
    ) -> None:
        self._retriever = retriever
        self._k = k
        self._documents = documents

    @property
    def state(self) -> State:
        return State.SEARCH_HISTORY

    async def run(self, ctx: InvestigationContext) -> Transition:
        query = self._query(ctx)

        try:
            result = await self._retriever.retrieve(query, self._k, self._filters(ctx))
        except Exception as exc:  # noqa: BLE001 - retrieval failing is not the run failing
            # The knowledge base being unavailable is a fact about the investigation, not about
            # the incident, and it must not end a run that still has live signals to reason over.
            note = f"knowledge base search failed: {type(exc).__name__}: {exc}"
            ctx.note(note)
            logger.warning("SEARCH_HISTORY: %s", note)

            return self._advance(ctx, message=note, payload={"failed": True})

        items = self._to_evidence(result.chunks)

        if not items:
            note = f"nothing in the knowledge base matched: {query}"
            ctx.note(note)

            return self._advance(ctx, message=note, payload={"documents": 0, "query": query})

        events = []

        for item in items:
            index = ctx.add_evidence(item)
            events.append(
                AgentEvent(
                    type=EventType.EVIDENCE_FOUND,
                    state=self.state,
                    message=item.summary,
                    payload={
                        "index": index,
                        "source": item.source,
                        "weight": item.weight,
                        "path": (item.raw or {}).get("path"),
                    },
                )
            )

        return self._advance(
            ctx,
            message=f"{len(items)} document(s) from the knowledge base via {result.retriever}",
            events=tuple(events),
            payload={
                "documents": len(items),
                "query": query,
                "retriever": result.retriever,
                "latency_ms": result.stage_latency_ms.get("total"),
            },
        )

    def _query(self, ctx: InvestigationContext) -> str:
        """What to search for.

        The question, plus the service when one is known and the question does not already say
        it. Everything the collectors have found is deliberately *not* mixed in: retrieval over a
        query stuffed with log lines matches documents that share log vocabulary rather than
        documents about the failure, and Phase 6 measured what happens when a query stops looking
        like a question.
        """
        service = ctx.target_service

        if service and service.lower() not in ctx.query.lower():
            return f"{ctx.query} ({service})"

        return ctx.query

    def _filters(self, ctx: InvestigationContext) -> dict[str, object] | None:
        """No filter by service, on purpose.

        Filtering to the incident's service would hide the architecture notes and the runbooks,
        which carry no service at all, and would hide the postmortem of the neighbouring service
        that is the other end of a cascade.
        """
        return None

    def _to_evidence(self, chunks: list[RetrievedChunk]) -> list[EvidenceItem]:
        """One fact per document, best chunk first, capped."""
        seen: dict[str, RetrievedChunk] = {}

        for chunk in chunks:
            seen.setdefault(chunk.chunk.document_id, chunk)

        items: list[EvidenceItem] = []

        for position, chunk in enumerate(list(seen.values())[: self._documents]):
            stored = chunk.chunk
            label = stored.external_id or stored.path or stored.title
            excerpt = " ".join(stored.content.split())[:200]

            items.append(
                EvidenceItem(
                    source=(
                        EvidenceSource.HISTORICAL_INCIDENT
                        if stored.source_type in _HISTORICAL
                        else EvidenceSource.RAG_DOCUMENT
                    ),
                    summary=f"{stored.source_type} {label}: {excerpt}",
                    weight=WEIGHT_DOCUMENT if position == 0 else WEIGHT_DOCUMENT_TAIL,
                    raw={
                        "chunk_id": stored.chunk_id,
                        "document_id": stored.document_id,
                        "title": stored.title,
                        "source_type": stored.source_type,
                        "service": stored.service,
                        "external_id": stored.external_id,
                        "path": stored.path,
                        "section": stored.section,
                        "content": stored.content,
                        "rank": chunk.rank,
                    },
                    tool=f"rag/{chunk.retriever}",
                )
            )

        return items

    def _advance(
        self,
        ctx: InvestigationContext,
        *,
        message: str,
        events: tuple[AgentEvent, ...] = (),
        payload: dict[str, object] | None = None,
    ) -> Transition:
        return Transition(
            next_state=ctx.advance() or State.GENERATE_HYPOTHESES,
            message=message,
            events=events,
            payload=payload,
        )
