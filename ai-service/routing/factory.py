"""Which router the service runs, decided in one place.

`agents/build.py` deliberately constructs none of its dependencies — the provider, the MCP
client, the retriever and the router are seams the benchmark and the tests swap. But something
has to choose, and before Phase 8 that choice was one line in `app/api/investigations.py`
reading `RuleBasedRouter()`. This is that line, given a name and the settings it depends on, so
the agent, the API and anything else that needs a router agree about what "the router" is.

**The keyword table does not leave when the model arrives.** It moves behind it. `ModelRouter`
falls back to whatever it is given whenever the model returns something unparseable or is not on
the machine at all, which means a checkout that has never run `ollama create sentinel-router`
keeps routing — at 0.354 rather than 0.871, and without failing a single investigation at its
first node. Silently degrading is the right behaviour here and it is only right because it is
visible: the plan event records `router`, so a run says which one answered.
"""

from __future__ import annotations

import logging

from app.config import Settings
from llm.ollama_provider import OllamaLlmProvider
from routing.base import Router
from routing.model_router import ModelRouter
from routing.rule_router import RuleBasedRouter

logger = logging.getLogger(__name__)

#: What a model-backed router is called in the plan event and in `evaluation_runs`. Not "model":
#: the benchmark runs an untuned model through the same class, and a row six months from now has
#: to say which of the two produced the decision.
FINE_TUNED = "fine_tuned"


def build_router(config: Settings) -> Router:
    """The tuned model in front of the keyword table, unless settings say otherwise.

    The provider is this function's own rather than the reasoning model's: the router is a 1.5B
    answering in about a second, and it has a timeout to match. Sharing the investigation's
    provider would point the router at the 7B and give it a two-minute cap.
    """
    if not config.router_use_model:
        logger.info("routing by keyword table: router_use_model is off")

        return RuleBasedRouter()

    provider = OllamaLlmProvider(
        base_url=config.ollama_base_url,
        model=config.router_model,
        timeout_seconds=config.router_timeout_seconds,
    )

    return ModelRouter(
        provider,
        name=FINE_TUNED,
        prompt_version=config.router_prompt_version,
        fallback=RuleBasedRouter(),
    )
