"""Scoring a query against a chunk with both of them in front of the model at once.

Retrieval and reranking answer different questions. A bi-encoder embeds the chunk before it has
ever seen the query, so the vector has to be a summary of everything the chunk might be asked
about; that is what makes it fast enough to search the whole corpus, and what makes it blunt. A
cross-encoder reads the pair together and outputs one relevance score, which is a far better
judgement and far too slow to run over a corpus — 30 pairs, not 900.

So the two compose: hybrid retrieval proposes 30 candidates in tens of milliseconds, and the
cross-encoder reorders them. Everything fusion got roughly right, the reranker gets exactly
right, and the chunk BM25 ranked twelfth because it happened to repeat a term ends up where it
belongs.

The model is BAAI/bge-reranker-v2-m3 — the reranker trained alongside bge-m3, sharing its
multilingual vocabulary, which is what preserves the Turkish-query-English-corpus property
Phase 5 relies on. It is a cross-encoder, so unlike the embedding model it cannot be served by
Ollama; docs/adr/0005-reranker-runs-in-process.md covers how it runs and why it is optional.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import math
import time
from typing import Any, Protocol

from rag.documents import RetrievedChunk

logger = logging.getLogger(__name__)

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-v2-m3"

# Pairs scored per forward pass. Cost is quadratic in sequence length and linear in batch, and
# on CPU — which is where this runs on a machine whose VRAM is holding the reasoning model — 16
# pairs of 512 tokens is about the largest batch that stays under a second.
DEFAULT_BATCH_SIZE = 16

# Tokens per (query, chunk) pair. bge-reranker-v2-m3 accepts 8192, and using it would waste an
# order of magnitude: chunks are ~400 tokens by construction, so 512 covers the query, the chunk
# and its heading breadcrumb with room left, and anything longer is padding the model pays
# attention over.
DEFAULT_MAX_LENGTH = 512


class RerankError(RuntimeError):
    """The reranker could not score the candidates."""


class RerankUnavailableError(RerankError):
    """The reranker is not installed, or its weights are not present.

    Separate from :class:`RerankError` for the reason the embedding provider separates them: the
    fix is an install or a download, not a different request.
    """


class CrossEncoderLike(Protocol):
    """The one method this module needs from a cross-encoder.

    Narrower than ``sentence_transformers.CrossEncoder`` on purpose. It is what lets the tests
    exercise ordering, batching and degradation without 2.5 GB of PyTorch in the environment,
    and it is the whole surface something else would have to implement to run the model another
    way — a TEI container, say.
    """

    # Positional-only, and with the two keyword arguments spelled out rather than ``**kwargs``:
    # the real library names the first argument ``sentences`` and takes a fixed keyword list, so
    # a protocol that fixed the name or demanded arbitrary keywords would exclude the one
    # implementation it exists to describe.
    def predict(
        self,
        pairs: list[tuple[str, str]],
        /,
        *,
        batch_size: int = ...,
        show_progress_bar: bool = ...,
    ) -> Any: ...


class Reranker(abc.ABC):
    """Something that reorders candidates by how well each answers the query."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Identifier used in evaluation tables and in ``retrieval_logs``."""

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Model identifier, as the runtime knows it."""

    @abc.abstractmethod
    async def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        k: int,
    ) -> list[RetrievedChunk]:
        """The ``k`` best of ``chunks`` for ``query``, best first, renumbered from rank 1.

        Raises:
            RerankUnavailableError: the model could not be loaded.
            RerankError: the model was loaded but did not produce usable scores.
        """

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Whether the model can currently be used. Must not raise."""


class CrossEncoderReranker(Reranker):
    """bge-reranker-v2-m3, loaded into this process by sentence-transformers.

    Two things about the implementation are worth knowing.

    *The model is loaded on first use, not at construction.* Constructing this is what the API
    layer does at import time, and a 2.2 GB download is not something to start while a health
    check is waiting. The first search that asks for a rerank pays for the load; the ones after
    it do not.

    *Scoring is offloaded to a thread and serialised.* ``predict`` is synchronous and CPU-bound,
    so a thread keeps the event loop answering. The lock is there because two concurrent batches
    on one CPU model do not go twice as fast — they interleave, both get slower, and the memory
    for both is live at once.
    """

    def __init__(
        self,
        model: str = DEFAULT_RERANK_MODEL,
        device: str | None = None,
        max_length: int = DEFAULT_MAX_LENGTH,
        batch_size: int = DEFAULT_BATCH_SIZE,
        encoder: CrossEncoderLike | None = None,
    ) -> None:
        self._model = model
        self._device = device
        self._max_length = max_length
        self._batch_size = batch_size
        self._encoder = encoder
        self._load_lock = asyncio.Lock()
        self._predict_lock = asyncio.Lock()
        self._load_error: str | None = None

    @property
    def name(self) -> str:
        return "cross_encoder"

    @property
    def model(self) -> str:
        return self._model

    @property
    def loaded(self) -> bool:
        return self._encoder is not None

    @property
    def unavailable_reason(self) -> str | None:
        """Why the last load attempt failed, for ``GET /rag/stats``.

        ``None`` before the first attempt as well as after a successful one, which is why stats
        reports it alongside availability rather than instead of it.
        """
        return self._load_error

    async def rerank(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        k: int,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []

        encoder = await self._ensure_loaded()
        pairs = [(query, chunk.content) for chunk in chunks]

        async with self._predict_lock:
            started = time.perf_counter()
            scores = await asyncio.to_thread(self._predict, encoder, pairs)
            logger.debug(
                "Reranked %d candidates in %d ms",
                len(pairs),
                int((time.perf_counter() - started) * 1000),
            )

        if len(scores) != len(chunks):
            raise RerankError(f"Asked the reranker for {len(chunks)} scores and got {len(scores)}.")

        return order(chunks, scores, k, retriever=self.name)

    def _predict(self, encoder: CrossEncoderLike, pairs: list[tuple[str, str]]) -> list[float]:
        raw = encoder.predict(pairs, batch_size=self._batch_size, show_progress_bar=False)

        try:
            scores = [float(value) for value in raw]
        except (TypeError, ValueError) as exc:
            raise RerankError(f"The reranker returned scores that are not numbers: {exc}") from exc

        return _to_probabilities(scores)

    async def _ensure_loaded(self) -> CrossEncoderLike:
        if self._encoder is not None:
            return self._encoder

        async with self._load_lock:
            # Checked again inside the lock: several searches can arrive before the first load
            # finishes, and loading the model twice is 2.2 GB twice.
            if self._encoder is not None:
                return self._encoder

            self._encoder = await asyncio.to_thread(self._load)
            self._load_error = None

        return self._encoder

    def _load(self) -> CrossEncoderLike:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:
            self._load_error = (
                "sentence-transformers is not installed. Run, in ai-service/: "
                "pip install -e .[rerank]"
            )
            raise RerankUnavailableError(self._load_error) from exc

        logger.info("Loading reranker %s (first use; this can take a while)", self._model)

        try:
            return CrossEncoder(self._model, device=self._device, max_length=self._max_length)
        except Exception as exc:
            # Anything from a failed download to a machine without the memory to hold the
            # weights. All of it means the same thing to a caller: no reranking this run.
            self._load_error = f"Could not load reranker '{self._model}': {exc}"
            raise RerankUnavailableError(self._load_error) from exc

    async def is_available(self) -> bool:
        try:
            await self._ensure_loaded()
        except RerankUnavailableError:
            return False
        except Exception as exc:  # pragma: no cover - defensive; is_available must not raise
            logger.warning("Unexpected error checking the reranker: %s", exc)
            return False

        return True


def order(
    chunks: list[RetrievedChunk],
    scores: list[float],
    k: int,
    retriever: str,
) -> list[RetrievedChunk]:
    """Sort candidates by score, best first, renumbering the ranks from one.

    Ties break on the rank the chunk arrived with, then on its id — the rule fusion uses, for
    the reason fusion uses it: a reranker that gives two chunks identical scores must not order
    them differently on two runs, or the evaluation measures the sort.
    """
    scored = sorted(
        zip(chunks, scores, strict=True),
        key=lambda pair: (-pair[1], pair[0].rank, pair[0].chunk_id),
    )[:k]

    return [
        chunk.reranked(rank=rank, score=score, retriever=retriever)
        for rank, (chunk, score) in enumerate(scored, start=1)
    ]


def _to_probabilities(scores: list[float]) -> list[float]:
    """Put scores on 0..1, whatever the library handed back.

    sentence-transformers applies a sigmoid to a single-logit cross-encoder on some versions and
    returns the raw logit on others. Ranking is unaffected either way — sigmoid is monotonic —
    but the number is written to ``retrieval_logs`` and shown in the evidence panel, and a
    relevance of ``6.31`` next to one of ``0.87`` in the same column is not readable.

    Applying it unconditionally would squash an already-squashed score into a narrow band around
    0.5, so it is applied only when something is outside the range.
    """
    if all(0.0 <= score <= 1.0 for score in scores):
        return scores

    return [_sigmoid(score) for score in scores]


def _sigmoid(value: float) -> float:
    # Split on the sign: exp(710) overflows and exp(-710) does not, so each branch is written to
    # take the exponent of a negative number.
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))

    exponential = math.exp(value)

    return exponential / (1.0 + exponential)
