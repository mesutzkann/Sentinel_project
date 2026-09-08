"""Turning text into the vectors the dense half of retrieval searches over.

The model is BAAI/bge-m3, chosen in docs/planning.md for one reason that matters here: it puts
Turkish and English in the same vector space, so a runbook written in English is retrievable by
a Turkish question about the same failure. 1024 dimensions, which is what ``rag.document_chunks``
is declared with.

It is served by Ollama rather than loaded in-process with sentence-transformers — see
docs/adr/0004-embeddings-through-ollama.md. The interface is what the rest of the code depends
on, so that decision is one class, not an assumption spread through the retrievers.
"""

from __future__ import annotations

import abc
import logging
import math
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Ollama loads the whole batch into memory before encoding. 32 is the batch size planning
# settled on: large enough that the per-request overhead disappears, small enough that ingesting
# a few hundred chunks does not compete with the 3B reasoning model for the same 6 GB of VRAM.
DEFAULT_BATCH_SIZE = 32


class EmbeddingError(RuntimeError):
    """The provider could not produce embeddings."""


class EmbeddingUnavailableError(EmbeddingError):
    """The embedding runtime is unreachable, or does not have the model.

    Separate for the same reason :class:`llm.base.LlmUnavailableError` is: the fix is
    ``ollama pull bge-m3``, not a different request.
    """


class EmbeddingProvider(abc.ABC):
    """Something that can turn text into vectors of a fixed width."""

    @property
    @abc.abstractmethod
    def model(self) -> str:
        """Model identifier as the runtime knows it."""

    @property
    @abc.abstractmethod
    def dimensions(self) -> int:
        """Vector width. Must match the column ``rag.document_chunks.embedding`` was created
        with — pgvector rejects a mismatch at insert rather than degrading silently."""

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed documents, in order. Returns one vector per input.

        Raises:
            EmbeddingUnavailableError: the runtime is unreachable or lacks the model.
            EmbeddingError: the runtime answered, but not with usable vectors.
        """

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query.

        A separate method because asymmetric models want a different prefix on the query side.
        bge-m3 is symmetric and needs none, so this is one call to :meth:`embed` — but the seam
        exists, and a provider that needs the prefix can add it without every caller learning
        which side it is on.
        """
        vectors = await self.embed([text])

        if not vectors:
            raise EmbeddingError("The provider returned no vector for the query.")

        return vectors[0]

    @abc.abstractmethod
    async def is_available(self) -> bool:
        """Whether the runtime is reachable and has the model. Must not raise."""


class OllamaEmbeddingProvider(EmbeddingProvider):
    """bge-m3 through the Ollama server that already serves the reasoning model."""

    def __init__(
        self,
        base_url: str,
        model: str = "bge-m3",
        dimensions: int = 1024,
        batch_size: int = DEFAULT_BATCH_SIZE,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._timeout = timeout_seconds
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []

        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))

        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        # Ollama rejects an empty string, and a chunk that is empty by the time it gets here is
        # a chunker bug rather than something to paper over — but a document whose last line is
        # whitespace should not fail an entire ingest, so it is padded to a space.
        payload: dict[str, Any] = {
            "model": self._model,
            "input": [text if text.strip() else " " for text in batch],
        }

        try:
            response = await self._post("/api/embed", payload)
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(
                f"Could not reach Ollama at {self._base_url}: {exc}"
            ) from exc

        if response.status_code == 404:
            raise EmbeddingUnavailableError(
                f"Ollama does not have model '{self._model}'. Run: ollama pull {self._model}"
            )

        if response.status_code >= 400:
            raise EmbeddingError(f"Ollama returned {response.status_code}: {response.text[:400]}")

        embeddings = response.json().get("embeddings") or []

        if len(embeddings) != len(batch):
            raise EmbeddingError(
                f"Asked Ollama for {len(batch)} embeddings and got {len(embeddings)}."
            )

        for vector in embeddings:
            if len(vector) != self._dimensions:
                # Caught here rather than at insert, where it surfaces as a pgvector type error
                # naming neither the model nor the configured width.
                raise EmbeddingError(
                    f"Model '{self._model}' returned {len(vector)} dimensions, "
                    f"but this store is configured for {self._dimensions}."
                )

        # Ollama normalises, but not on every version and not for every model. Cosine distance
        # in pgvector does not require unit vectors; the dot-product shortcut the retrievers
        # could later use does, and a silently un-normalised vector would make that change a
        # subtle ranking bug rather than an error.
        return [normalize(v) for v in embeddings]

    async def is_available(self) -> bool:
        try:
            response = await self._get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Ollama is not reachable at %s: %s", self._base_url, exc)
            return False

        installed = {m.get("name", "") for m in response.json().get("models", [])}

        if self._model in installed or f"{self._model}:latest" in installed:
            return True

        logger.warning(
            "Ollama is up but does not have embedding model '%s'. Run: ollama pull %s",
            self._model,
            self._model,
        )
        return False

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(f"{self._base_url}{path}", json=payload)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(f"{self._base_url}{path}", json=payload)

    async def _get(self, path: str) -> httpx.Response:
        if self._client is not None:
            return await self._client.get(f"{self._base_url}{path}")

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(f"{self._base_url}{path}")


def normalize(vector: list[float]) -> list[float]:
    """Scale to unit length, leaving a zero vector alone rather than dividing by zero."""
    norm = math.sqrt(sum(v * v for v in vector))

    if norm == 0.0:
        return list(vector)

    return [v / norm for v in vector]
