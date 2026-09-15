"""Measuring retrieval without every retriever having to know it is measured.

A decorator rather than a line inside each of the four ``Retriever`` implementations, and rather
than a line at each call site. The reason is the same one that keeps ``rag_eval`` out of
``retrieval_logs``: a benchmark runs 240 searches back to back over a fixed question set, and
folding those into the same histogram as the live ones would move the p95 the dashboard reports
by whatever the last benchmark happened to measure.

Wrapping is therefore where *live* is decided — ``RagService``, which is the one retriever set
both the /rag endpoints and the agent's SEARCH_HISTORY node draw from. Anything that builds its
own retrievers, which is to say the evaluation, is unmeasured by construction rather than by
remembering to pass a flag.
"""

from __future__ import annotations

import time
from typing import Any

from observability.instruments import OUTCOME_ERROR, record_retrieval
from rag.retrievers import RetrievalResult, Retriever


class InstrumentedRetriever(Retriever):
    """Records how long a search took, then returns exactly what the inner retriever returned."""

    def __init__(self, inner: Retriever) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        """The inner retriever's name.

        Passed straight through rather than decorated. This name is written to
        ``retrieval_logs``, compared against evaluation rows and shown in the evidence panel, so
        an ``instrumented(hybrid_rerank)`` here would make the live rows fail to join against the
        benchmark ones.
        """
        return self._inner.name

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> RetrievalResult:
        started = time.perf_counter()

        try:
            result = await self._inner.retrieve(query, k, filters)
        except Exception:
            # Re-raised: the caller decides what a failed search means, and for the agent that
            # decision is already made — SEARCH_HISTORY notes it and carries on. The measurement
            # is taken on the way past, because that decision is exactly what would otherwise
            # make this failure invisible.
            record_retrieval(
                retriever=self.name,
                duration_ms=int((time.perf_counter() - started) * 1000),
                outcome=OUTCOME_ERROR,
            )
            raise

        # The retriever already measured itself, and its number is the better one: wall clock
        # across the whole search, as against the sum of two halves that ran concurrently.
        # ``result.retriever`` rather than ``self.name``, because a reranker that degraded to the
        # fused order returns results under the name of what actually served them.
        record_retrieval(retriever=result.retriever, duration_ms=result.total_latency_ms)

        return result
