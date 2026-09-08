"""The SentinelAI reasoning service.

Phase 3 is the LLM layer only: a provider abstraction, structured output, and a prompt registry.
The agent state machine, hybrid RAG and the MCP client arrive in later phases and mount onto the
same app.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api import llm as llm_api
from app.api import mcp as mcp_api
from app.config import settings
from llm.prompts import registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

app = FastAPI(
    title="SentinelAI AI Service",
    version="0.1.0",
    description=(
        "Local LLM access, structured output, the prompt registry, and the MCP tool "
        "registry the agent reads the running system through. Investigations and retrieval "
        "arrive in later phases."
    ),
)

# The MCP Tools page calls this service directly rather than through the backend, so it is a
# cross-origin caller and needs to be allowed explicitly. Without this the page fails in a
# browser while every curl against the same endpoints succeeds — a difference that is easy to
# miss and hard to read when it bites.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(llm_api.router)
app.include_router(mcp_api.router)


class Health(BaseModel):
    status: str
    model: str
    prediction_reporting: bool


class PromptSummary(BaseModel):
    id: str
    name: str
    version: str
    variables: list[str]


@app.get("/health", response_model=Health, tags=["meta"])
async def health() -> Health:
    """Liveness only.

    Says nothing about Ollama on purpose: whether the model runtime is up is a separate
    question with its own endpoint, and folding it in here would make the service look down
    whenever a dependency was.
    """
    config = settings()

    return Health(
        status="up",
        model=config.llm_model,
        prediction_reporting=config.prediction_reporting_enabled,
    )


@app.get("/prompts", response_model=list[PromptSummary], tags=["llm"])
async def prompts() -> list[PromptSummary]:
    """Every registered prompt, so a caller can see what is available without reading the repo."""
    return [
        PromptSummary(
            id=p.id,
            name=p.name,
            version=p.version,
            variables=sorted(p.variables),
        )
        for p in registry().all()
    ]
