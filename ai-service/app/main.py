"""The SentinelAI reasoning service.

Four surfaces, each mounted as its own router: the LLM layer from Phase 3, the MCP tool registry
from Phase 4, hybrid retrieval from Phase 5, and — from Phase 7 — investigations, which is the
one that does the work. It adds no capability of its own: it starts the agent, and the agent
consumes the other three.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.api import actions as actions_api
from app.api import investigations as investigations_api
from app.api import llm as llm_api
from app.api import mcp as mcp_api
from app.api import models as models_api
from app.api import rag as rag_api
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
        "Local LLM access, structured output, the prompt registry, the MCP tool registry "
        "the agent reads the running system through, hybrid retrieval over the knowledge base, "
        "and the investigation agent that uses all of them."
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
app.include_router(rag_api.router)
app.include_router(investigations_api.router)
app.include_router(models_api.router)
app.include_router(actions_api.router)


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
