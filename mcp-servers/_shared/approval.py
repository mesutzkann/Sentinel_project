"""Verifying an approval, on the server side of the boundary.

The AI service's policy already refuses a destructive call without a valid approval. This checks
it again, in the container that would actually do the thing, and the duplication is the point:
these servers listen on a compose network, and a tool that restarts containers must not be
callable by anything that can reach port 7007. An approval boundary enforced only by the caller
is a boundary enforced by whoever the caller happens to be.

**This half only verifies.** Tokens are issued by the backend when a person approves a specific
recommendation; a server that could mint one would be a server that could approve its own work.

The format is the one in `ai-service/mcp_client/approval.py`, and the two are kept in step by
`tests/test_approval.py` here and `tests/test_mcp_approval.py` there sharing fixed vectors — a
wire format described in prose in two places is one that will drift.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from _shared.config import settings

logger = logging.getLogger(__name__)

#: The argument a destructive tool carries its approval in, and the one key excluded from the
#: hash the approval is bound to: a token cannot be part of what it authorises.
APPROVAL_ARGUMENT = "approval_token"

SCHEME = "sa1"


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    valid: bool
    reason: str
    recommendation_id: str | None = None
    approved_by: str | None = None


def hash_arguments(arguments: dict[str, Any] | None) -> str:
    scoped = {k: v for k, v in (arguments or {}).items() if k != APPROVAL_ARGUMENT}

    return hashlib.sha256(
        json.dumps(scoped, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def verify(
    token: str | None,
    *,
    tool: str,
    arguments: dict[str, Any] | None = None,
    secret: str | None = None,
) -> ApprovalCheck:
    """Whether this token authorises this call, on this server.

    ``tool`` is the qualified ``server/tool`` name the approval was given for, and the arguments
    are the ones about to be acted on. Both come from the call rather than from the token: a
    check that read them out of the token would only be asking whether the token is well formed.
    """
    key = (secret if secret is not None else settings().approval_secret).encode("utf-8")

    if not key:
        # The closed default, same as the AI service's: a server with no secret configured
        # cannot perform destructive work at all.
        return ApprovalCheck(False, "This server has no approval secret configured.")

    if not token or token.count(".") != 1:
        return ApprovalCheck(False, "The approval token is not in the expected form.")

    payload, signature = token.split(".", 1)
    expected = _b64(hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest())

    if not hmac.compare_digest(signature, expected):
        return ApprovalCheck(False, "The approval token's signature is not valid.")

    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, json.JSONDecodeError):
        return ApprovalCheck(False, "The approval token could not be read.")

    if not isinstance(claims, dict) or claims.get("v") != SCHEME:
        return ApprovalCheck(False, "The approval token is of a scheme this server does not take.")

    if int(claims.get("exp", 0)) <= int(time.time()):
        return ApprovalCheck(False, "The approval has expired. Approve the action again.")

    if claims.get("tool") != tool:
        return ApprovalCheck(
            False, f"The approval is for '{claims.get('tool')}' and this call is to '{tool}'."
        )

    if claims.get("args") != hash_arguments(arguments):
        return ApprovalCheck(
            False, f"The approval for '{tool}' was given for different arguments."
        )

    return ApprovalCheck(
        True,
        "Approved.",
        recommendation_id=str(claims.get("rec", "")) or None,
        approved_by=claims.get("by"),
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
