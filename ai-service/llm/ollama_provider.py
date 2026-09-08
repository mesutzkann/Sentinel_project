"""Ollama behind :class:`LocalLlmProvider`."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from llm.base import (
    LlmCompletion,
    LlmError,
    LlmMessage,
    LlmOptions,
    LlmUnavailableError,
    LocalLlmProvider,
)

logger = logging.getLogger(__name__)


class OllamaLlmProvider(LocalLlmProvider):
    """Talks to an Ollama server over its HTTP API.

    Uses ``/api/chat`` rather than ``/api/generate`` so system and user turns stay separate
    fields instead of being concatenated into one string with hand-rolled delimiters, which is
    what the instruction-tuned models are trained to expect.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        # Generous, because a 7B model answering on CPU is genuinely slow, and a timeout that
        # fires mid-investigation is a worse failure than a slow one.
        self._timeout = timeout_seconds
        self._client = client

    @property
    def model(self) -> str:
        return self._model

    async def complete(
        self,
        messages: list[LlmMessage],
        options: LlmOptions | None = None,
    ) -> LlmCompletion:
        options = options or LlmOptions()
        payload = self._build_payload(messages, options)

        started = time.perf_counter()

        try:
            response = await self._post("/api/chat", payload)
        except httpx.HTTPError as exc:
            raise LlmUnavailableError(
                f"Could not reach Ollama at {self._base_url}: {exc}"
            ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code == 404:
            # Ollama answers 404 for a model it does not have. Distinct from a transport failure:
            # the fix is `ollama pull`, not restarting anything.
            raise LlmUnavailableError(
                f"Ollama does not have model '{self._model}'. Run: ollama pull {self._model}"
            )

        if response.status_code >= 400:
            raise LlmError(
                f"Ollama returned {response.status_code}: {response.text[:400]}"
            )

        body = response.json()
        text = body.get("message", {}).get("content", "")

        if not text:
            raise LlmError("Ollama returned an empty completion.")

        return LlmCompletion(
            text=text,
            model=body.get("model", self._model),
            # Absent on some Ollama versions and for cached prompts. Reported as zero rather
            # than guessed, so a zero in model_predictions means "not reported" rather than a
            # number this code invented.
            prompt_tokens=int(body.get("prompt_eval_count") or 0),
            completion_tokens=int(body.get("eval_count") or 0),
            latency_ms=elapsed_ms,
        )

    async def is_available(self) -> bool:
        try:
            response = await self._get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Ollama is not reachable at %s: %s", self._base_url, exc)
            return False

        installed = {m.get("name", "") for m in response.json().get("models", [])}

        # Ollama reports `qwen2.5:3b-instruct`; a request for `qwen2.5` resolves to
        # `qwen2.5:latest`. Compare on both, so a configured name without an explicit tag is
        # not reported as missing.
        if self._model in installed or f"{self._model}:latest" in installed:
            return True

        logger.warning(
            "Ollama is up but does not have model '%s'. Installed: %s",
            self._model,
            sorted(installed) or "none",
        )
        return False

    def _build_payload(
        self,
        messages: list[LlmMessage],
        options: LlmOptions,
    ) -> dict[str, Any]:
        model_options: dict[str, Any] = {
            "temperature": options.temperature,
            "top_p": options.top_p,
        }

        if options.max_tokens is not None:
            model_options["num_predict"] = options.max_tokens

        if options.stop:
            model_options["stop"] = options.stop

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # One response, not a token stream. Nothing here renders progressively, and a single
            # response keeps the token counts in the same body as the text.
            "stream": False,
            "options": model_options,
        }

        if options.json_schema is not None:
            # Ollama constrains decoding to the schema, so malformed JSON becomes impossible
            # rather than merely unlikely. Older servers treat an unrecognised value as plain
            # JSON mode, which is why the caller still validates the result.
            payload["format"] = options.json_schema

        return payload

    async def _post(self, path: str, payload: dict[str, Any]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                f"{self._base_url}{path}", json=payload, timeout=self._timeout
            )

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.post(f"{self._base_url}{path}", json=payload)

    async def _get(self, path: str) -> httpx.Response:
        if self._client is not None:
            return await self._client.get(f"{self._base_url}{path}", timeout=self._timeout)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(f"{self._base_url}{path}")
