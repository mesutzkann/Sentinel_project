"""The ``/actions`` endpoints: run what a person approved, and report what it did.

docs/planning.md §3 step 7: the backend posts here when somebody clicks approve, carrying the
token it issued for that recommendation. This is the only way into a destructive tool from
outside the agent, and it is deliberately narrow — it takes a tool name, the arguments the
approval was given for, and the token, and it does not decide anything.

**It answers when the verification is done, not when the tool returns.** A remediation that
reports "restarted" and stops has said what it did rather than what happened, so the call waits
out a settle window and measures the symptom again. That makes this endpoint slow by the
standards of an HTTP API — half a minute — and it is the right trade for the one caller it has:
the backend is recording an outcome, not rendering a page, and a second round trip to ask "did
it work" is a second place for the answer to be lost.

**Nothing here is persisted.** The backend owns `recommendations` and the row that says an action
was executed; this service does the work and answers. Two stores would be two answers to whether
an incident was remediated.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, StringConstraints

from agents.remediation import (
    DEFAULT_SETTLE_SECONDS,
    VERIFY_WINDOW_MINUTES,
    Remediator,
    Verdict,
)
from app.api.mcp import get_client, get_policy, get_registry
from app.config import Settings, settings
from mcp_client.client import McpClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/actions", tags=["actions"])


# ---------------------------------------------------------------------- contracts ----


class ExecuteRequest(BaseModel):
    """What the backend sends when a person approves a recommendation."""

    tool: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=120)] = (
        Field(description="`server/tool`, exactly as the approval names it.")
    )

    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The arguments the approval was given for. Anything else — an added key, a changed "
            "value — makes the approval invalid, which is the point of it."
        ),
    )

    approval_token: str = Field(
        min_length=8,
        description="Issued by the backend for this recommendation when a human approved it.",
    )

    service: str | None = Field(
        default=None,
        description=(
            "The service whose symptom this is meant to fix. Without it the action still runs "
            "and nothing is verified, because there is nothing to measure."
        ),
    )

    investigation_id: str | None = None

    verify: bool = Field(
        default=True,
        description="Off skips the settle and the measurement, and reports the verdict unknown.",
    )

    settle_seconds: int | None = Field(
        default=None,
        ge=0,
        le=300,
        description=f"How long to wait before measuring again. Default {DEFAULT_SETTLE_SECONDS}s.",
    )


class VerificationResponse(BaseModel):
    verdict: Verdict
    summary: str
    service: str | None = None
    error_rate_before: float | None = None
    error_rate_after: float | None = None
    settled_seconds: int = 0
    window_minutes: int = VERIFY_WINDOW_MINUTES


class ExecuteResponse(BaseModel):
    recommendation_id: str
    tool: str
    executed: bool
    verification: VerificationResponse
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any = None
    error: str | None = None
    latency_ms: int = 0
    approved_by: str | None = None

    #: True only when the action ran *and* the measurement improved. The field exists so a caller
    #: cannot read `executed` as "it worked" — those are different questions and this phase is
    #: about the second one.
    confirmed: bool = False


# ------------------------------------------------------------------ dependencies ----


def get_settings() -> Settings:
    return settings()


async def get_mcp_client(config: Annotated[Settings, Depends(get_settings)]) -> McpClient:
    """The same client, policy and registry every other tool call goes through.

    Discovery is cached for the process and the first caller pays for it — a destructive tool
    that is not in the registry is refused as unknown, which is the correct answer for a tool
    whose annotations nobody has read.
    """
    registry = get_registry(config)

    if not registry.tools:
        await registry.discover()

    return get_client(registry, get_policy(registry, config), config)


# ---------------------------------------------------------------------- endpoints ----


@router.post("/{recommendation_id}/execute", response_model=ExecuteResponse)
async def execute(
    recommendation_id: str,
    request: ExecuteRequest,
    config: Annotated[Settings, Depends(get_settings)],
) -> ExecuteResponse:
    """Execute one approved action and measure whether it helped.

    The approval is checked by the policy layer and again by the server that does the work; this
    endpoint does not inspect the token itself. What it does check is that the service is
    configured to accept approvals at all — without a secret, every call here would be refused
    one layer down with a message about tokens, where the real answer is that this deployment has
    no approval path configured.
    """
    if not config.approval_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "This service has no APPROVAL_SECRET configured, so no approved action can be "
                "executed. Set it to the same value the backend signs approvals with."
            ),
        )

    client = await get_mcp_client(config)
    remediator = Remediator(
        client,
        settle_seconds=(
            request.settle_seconds
            if request.settle_seconds is not None
            else DEFAULT_SETTLE_SECONDS
        ),
    )

    execution = await remediator.execute(
        recommendation_id=recommendation_id,
        tool=request.tool,
        arguments=request.arguments,
        approval_token=request.approval_token,
        service=request.service,
        verify=request.verify,
    )

    logger.info(
        "Action %s (%s) executed=%s verdict=%s for investigation %s",
        recommendation_id,
        request.tool,
        execution.executed,
        execution.verification.verdict,
        request.investigation_id or "-",
    )

    payload = execution.to_payload()

    return ExecuteResponse(
        **{key: value for key, value in payload.items() if key != "verification"},
        verification=VerificationResponse(**payload["verification"]),
    )
