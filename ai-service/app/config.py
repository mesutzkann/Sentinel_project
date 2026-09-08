"""Settings, read from the environment and from ``.env`` at the repository root."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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

    # Development default is the 3B model. A 7B at Q4 does not comfortably share 6 GB of VRAM
    # with the rest of the stack, so 7B is switched on for demos and benchmark runs instead.
    llm_model: str = "qwen2.5:3b-instruct"

    llm_timeout_seconds: float = 120.0

    # Attempts, not retries. Constrained decoding usually makes the first one enough.
    llm_max_attempts: int = 3

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
    def prediction_reporting_enabled(self) -> bool:
        return bool(self.backend_base_url and self.internal_api_key)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()
