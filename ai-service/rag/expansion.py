"""Searching for the answer instead of for the question.

A question and the document that answers it are written by different people for different
purposes, and they often share almost no vocabulary. Three of the benchmark's measured failures
are exactly that gap:

* "how do I query the logs of a service, and what is the label called" never reached the
  observability document, which does not contain the word *label* — it contains
  ``{service_name="orders"}``.
* "notifications servisi gece neden çöktü" reached the notifications service doc rather than
  the postmortem of the night it ran out of memory.
* "ödeme servisinde daha önce kilitlenme yaşadık mı" did not reach the deadlock postmortem,
  because the bridge from *kilitlenme* to ``40P01: deadlock detected`` is not one an embedding
  of the question alone can cross.

HyDE (Gao et al., *Precise Zero-Shot Dense Retrieval without Relevance Labels*) closes it by
asking the local model to write the passage that *would* answer the question, and searching with
that. The generated passage is very likely wrong on the facts — it is invented — and that does
not matter: nothing generated is ever shown to a user or used as evidence. It is a probe, and it
is a good probe because it is written in the vocabulary of the corpus rather than of the asker.

Two decisions are worth stating.

*Only the dense half is expanded.* BM25 keeps the literal query. The lexical retriever exists to
find ``40P01`` and ``Retry__MaxAttempts``, and burying those tokens in three sentences of
invented prose is precisely how to stop it doing that.

*The hypothesis does not replace the query — it is fused with it.* Both are searched and the two
rankings are combined by reciprocal rank, the same way BM25 and dense are. A hypothesis that
wanders off topic then costs a ranking rather than the answer, which matters because a 3B model
asked about an unfamiliar service will occasionally write about a different one.

**Measured, and switched off.** It did what it was built for: three of the four failures above
were rescued, ``Q047`` from unfound to rank 4, ``Q042`` from rank 4 to rank 2, ``Q014`` from 5 to
4. Over the 120-query benchmark eleven queries improve. Eight get worse, one of them ``Q035``
from rank 1 to unfound — the fusion protects a wandering hypothesis from costing the answer, but
not from costing the top of the ranking. R@5 ties plain fusion at 0.944, R@3 is worse (0.890
against 0.910), and it costs eleven times the latency.

The decisive number is not that one, though: the cross-encoder closes the same gap better and
four times cheaper. ``Q047`` unfound to rank 3 against HyDE's 4, ``Q014`` 5 to 3 against 4,
``Q038`` 4 to 3 where HyDE left it at 4. Only ``Q042`` is a query HyDE places higher. Generating
a probe was the expensive way to reach a document whose vocabulary the question did not share.

So ``HYDE_ENABLED`` is false and this module stays: ``rag_eval --hyde`` reproduces the row and
``"retriever": "hybrid_hyde"`` runs it for one search. The corpus is 28 documents; a bigger one,
or a bigger generator, is a different measurement and this is the code that takes it.
"""

from __future__ import annotations

import abc
import logging
import time
from dataclasses import dataclass

from llm.base import LlmError, LlmMessage, LlmOptions, LocalLlmProvider
from llm.prompts import PromptRegistry, registry

logger = logging.getLogger(__name__)

# Enough for four sentences of prose. Longer probes do not retrieve better — the embedding is a
# fixed-width average, so a passage that keeps going dilutes the terms that made it useful — and
# every token is latency on a local model.
DEFAULT_MAX_TOKENS = 200

# A search should not wait on generation for longer than a person waits for a search. When the
# model is slow or busy, the expansion is dropped and the original query answers alone.
DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class Expansion:
    """A generated probe, and what it cost."""

    text: str
    model: str
    prompt_id: str
    latency_ms: int

    @property
    def is_usable(self) -> bool:
        # A one-word answer is a model that misunderstood the instruction, and embedding it
        # would add a ranking built on noise to the fusion.
        return len(self.text.split()) >= 8


class QueryExpander(abc.ABC):
    """Something that turns a query into an additional text to search with."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Identifier for evaluation rows and retrieval logs."""

    @abc.abstractmethod
    async def expand(self, query: str) -> Expansion | None:
        """The probe for ``query``, or ``None`` if there is not a usable one.

        Must not raise. An expander that fails has cost the search a little time; an expander
        that propagates has cost it the answer, and the original query was always sufficient.
        """


class HydeExpander(QueryExpander):
    """Asks the local model for the passage that would answer the question."""

    def __init__(
        self,
        provider: LocalLlmProvider,
        prompts: PromptRegistry | None = None,
        version: str = "v1",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider
        self._prompts = prompts or registry()
        self._version = version
        self._max_tokens = max_tokens
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return "hyde"

    async def expand(self, query: str) -> Expansion | None:
        prompt = self._prompts.get("hyde_passage", self._version)
        started = time.perf_counter()

        try:
            completion = await self._provider.complete(
                [LlmMessage(role="user", content=prompt.render(query=query))],
                LlmOptions(temperature=0.0, max_tokens=self._max_tokens),
            )
        except LlmError as exc:
            # Includes the runtime being down. Retrieval still works — this is the half of the
            # dense search that is an optimisation, and the fused result without it is the
            # Phase 5 behaviour rather than a failure.
            logger.warning("Query expansion skipped: %s", exc)
            return None
        except Exception as exc:  # pragma: no cover - expand() must not raise
            logger.warning("Query expansion failed unexpectedly: %s", exc)
            return None

        expansion = Expansion(
            text=_clean(completion.text),
            model=completion.model,
            prompt_id=prompt.id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

        if not expansion.is_usable:
            logger.warning("Query expansion produced nothing usable for %r", query)
            return None

        return expansion


def _clean(text: str) -> str:
    """Strip the wrappers a chat model adds around a passage it was asked for bare.

    Cheap and specific: a leading "Sure, here is…" line, a code fence, surrounding quotes. Not a
    parser — anything more elaborate belongs in the prompt, and the prompt already says not to
    do these things.
    """
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = [line for line in cleaned.splitlines() if not line.startswith("```")]
        cleaned = "\n".join(lines).strip()

    first, _, rest = cleaned.partition("\n")

    if rest and first.rstrip().endswith(":") and len(first.split()) <= 12:
        cleaned = rest.strip()

    return cleaned.strip('"').strip()
