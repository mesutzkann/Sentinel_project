"""Settings, read from the environment and from ``.env`` at the repository root."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

# ai-service/app/config.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Configuration for the AI service.

    Reads the same ``.env`` the compose stack and the backend use, so there is one place where
    the model name and the database live rather than three that can disagree.
    """

    model_config = SettingsConfigDict(
        env_file=_REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # LLM_MODEL rather than SENTINEL_LLM_MODEL: these names are shared with the rest of the
        # stack, so they are not namespaced to this service.
        env_prefix="",
    )

    ai_service_port: int = 8000

    # ---- LLM ----
    ollama_base_url: str = "http://localhost:11434"

    # The 3B is the default, and as of 2026-09-11 that is the more conservative choice rather
    # than the measured one. `python -m evaluation.reasoning_eval` over the five implemented
    # chaos scenarios, on the machine this is developed on (RTX 3060 Laptop, 6 GB), after the
    # critic was rebuilt (docs/adr/0007):
    #
    #   3B: 3 of 5 root causes correct, 2 of 5 investigations finished, 4.0 calls,  74 s
    #   7B: 4 of 5 root causes correct, 4 of 5 investigations finished, 4.4 calls,  90 s
    #
    # The 7B still does not fit: 5.12 GB of weights against 6 GB of VRAM, so Ollama places 82%
    # of it on the GPU and runs the rest on the CPU. What changed is that this is no longer
    # fatal. The September benchmark recorded three of five runs blowing a 900 s per-call
    # timeout; nothing here reproduces that — twice, including a run with bge-m3 loaded first,
    # where Ollama evicted the embedding model and the investigations took 140 s rather than 90.
    # The old figure was measured with the 3B run immediately before it in the same invocation
    # and before the schema and critic work cut a run from 6 model calls to 4.4; which of those
    # explains it has not been established.
    #
    # The decision, taken on those numbers: **the 3B to develop against, the 7B to demo with** —
    # the phase's original plan, which the September measurement had ruled out. A demo run sets
    # LLM_MODEL=qwen2.5:7b-instruct and LLM_TIMEOUT_SECONDS=300 in .env; the second of those is
    # not optional, because a 120 s cap against calls that averaged 43 s under contention ends an
    # investigation in FAILED for being slow, which is the one way to make a good model look
    # broken.
    llm_model: str = "qwen2.5:3b-instruct"

    llm_timeout_seconds: float = 120.0

    # Attempts, not retries. Constrained decoding usually makes the first one enough.
    llm_max_attempts: int = 3

    # ---- PostgreSQL ----
    # The same instance and the same credentials the backend and the samples use; this service
    # owns the `rag` schema inside it. Defaults are the compose values, so a natively run
    # service works against a `docker compose --profile core up` with no extra configuration.
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "sentinel"
    postgres_user: str = "sentinel"
    postgres_password: str = "sentinel_dev_pw"

    # ---- RAG ----
    embedding_model: str = "bge-m3"

    # Must match the width `rag.document_chunks.embedding` was migrated with. Changing it means
    # a migration and a re-ingest, not a restart.
    embedding_dimensions: int = 1024

    # 400 tokens with 60 of overlap, from docs/planning.md.
    chunk_target_tokens: int = 400
    chunk_overlap_tokens: int = 60

    # How many candidates each half of a hybrid search contributes before fusion, and how many
    # fused candidates the reranker is given to choose five from.
    retrieval_candidates: int = 30

    # At most this many chunks of one document in a result. Adjacent chunks of a runbook score
    # alike, so without a cap one document takes three of five slots and the second document the
    # question needed falls outside them. 0 disables it. See rag/retrievers.py.
    retrieval_max_chunks_per_document: int = 2

    # ---- Reranking ----
    # The cross-encoder is an optional dependency: it is ~2.5 GB of PyTorch and a 2.2 GB model,
    # and a search still answers without it (docs/adr/0005-reranker-runs-in-process.md). Empty
    # device means "whatever sentence-transformers picks" — CUDA when there is a GPU free.
    #
    # Empty dtype means "decide from the device": float16 on CUDA, float32 elsewhere. That
    # choice is worth 1.6 s a search on this machine and costs nothing measurable in recall;
    # rag/rerank.py carries the three measurements. Set it to float32 to check that for
    # yourself, or when a GPU old enough to lack fast half precision is what is available.
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_device: str = ""
    rerank_dtype: str = ""
    rerank_max_length: int = 512
    rerank_batch_size: int = 32

    # Tokens the retrieved context may occupy in a prompt, from docs/planning.md.
    context_token_budget: int = 3000

    # ---- Query expansion (HyDE) ----
    # Ask the local model for the passage that would answer the question and search with that
    # too, to close vocabulary gaps the question alone cannot cross. See rag/expansion.py.
    #
    # Off because it was measured, not because it was untried. It does what it was built to do —
    # it rescued three of the four queries that motivated it — and it does not pay for itself:
    # over 120 queries R@5 ties the plain hybrid at 0.944, R@3 is worse (0.890 against 0.910),
    # eleven queries improve and eight get worse, and p50 is 5.8 s against 518 ms. The reranker
    # closes the same vocabulary gap better and four times cheaper (rag/expansion.py).
    # Turning this on adds a `hybrid_hyde` retriever rather than changing `hybrid`.
    hyde_enabled: bool = False
    hyde_max_tokens: int = 200
    hyde_timeout_seconds: float = 20.0

    # Where the seed corpus lives, relative to the repository root.
    knowledge_base_path: str = "datasets/knowledge"

    # ---- Backend ----
    # Where model_predictions are reported. Empty disables reporting, which keeps the service
    # usable on its own without silently pretending the rows were written.
    backend_base_url: str = "http://localhost:5080"
    internal_api_key: str = ""

    # ---- MCP servers ----
    # Defaults are the published loopback ports, so the service works when run natively. Inside
    # compose these are overridden with the container hostnames.
    logs_mcp_url: str = "http://localhost:7001/mcp"
    metrics_mcp_url: str = "http://localhost:7002/mcp"
    traces_mcp_url: str = "http://localhost:7003/mcp"
    database_mcp_url: str = "http://localhost:7004/mcp"
    git_mcp_url: str = "http://localhost:7005/mcp"
    source_code_mcp_url: str = "http://localhost:7006/mcp"

    # Origins allowed to call this service from a browser. The Vite dev server by default.
    cors_origins: str = "http://localhost:5173"

    # Secret a destructive tool call must carry. Empty means no destructive call can be
    # approved, which is the correct default and the only one Phase 4 needs.
    approval_secret: str = ""

    @property
    def rerank_device_or_auto(self) -> str | None:
        """``None`` rather than an empty string, which sentence-transformers reads as a device."""
        return self.rerank_device or None

    @property
    def prediction_reporting_enabled(self) -> bool:
        return bool(self.backend_base_url and self.internal_api_key)

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the async driver the store and the migrations both use.

        Assembled from the same POSTGRES_* variables compose passes to everything else rather
        than read from a DATABASE_URL of its own: one password in .env, not two that can drift
        apart and produce an authentication failure nobody can locate.
        """
        return (
            f"postgresql+asyncpg://{quote(self.postgres_user)}:"
            f"{quote(self.postgres_password)}@{self.postgres_host}:{self.postgres_port}/"
            f"{self.postgres_db}"
        )

    @property
    def knowledge_base_dir(self) -> Path:
        """Absolute path to the seed corpus."""
        return _REPO_ROOT / self.knowledge_base_path


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
