"""The token a human's approval turns into, and what it is allowed to authorise.

Phase 4 shipped a placeholder: a shared secret, compared for equality. That is enough to prove
the policy path works and it is not enough to ship, because it authorises *everything*. A token
that says only "a human approved something" would let an approval of "restart the orders
container" be replayed as "drop the payments table" — the boundary would be real and the thing
it bounds would be the wrong set.

So a grant names the action:

    recommendation | server/tool | a hash of the arguments | an expiry

signed with HMAC-SHA256 and checked against the call actually being made. The verifier is given
the tool and the arguments at the moment of the call and compares them to the ones a person saw
on screen; anything else is refused with a reason that says which part did not match.

**Derived, never stored.** The same choice ADR-0002 makes for callback tokens, for the same
reason: a signed token needs no row, no lookup and no cleanup, and the AI service can verify one
without asking the backend whether it is still good. What it costs is revocation — a token
cannot be withdrawn before it expires, which is why the expiry is minutes rather than hours.

**The arguments are hashed, not carried.** They can contain a connection string or a patch; a
token is a thing that travels in logs, in URLs and over a callback. The hash binds the call
without quoting it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

#: How long an approval stays usable. Short on purpose: this is the window in which a decision a
#: person made in front of a screen still describes the system they were looking at. A restart
#: approved twenty minutes ago is being applied to a different incident.
DEFAULT_TTL_SECONDS = 600

#: Version tag in the payload. A token issued by an older scheme must fail verification rather
#: than be interpreted by a newer one — the failure mode of a silently reinterpreted security
#: token is that it authorises something nobody approved.
SCHEME = "sa1"


class ApprovalFailure(StrEnum):
    MALFORMED = "malformed"
    BAD_SIGNATURE = "bad_signature"
    EXPIRED = "expired"
    WRONG_TOOL = "wrong_tool"
    WRONG_ARGUMENTS = "wrong_arguments"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """What a person approved."""

    recommendation_id: str
    tool: str
    arguments_hash: str
    expires_at: int
    approved_by: str | None = None

    @property
    def expired(self) -> bool:
        return self.expires_at <= int(time.time())


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """The answer to "may this exact call proceed"."""

    valid: bool
    reason: str
    failure: ApprovalFailure | None = None
    grant: ApprovalGrant | None = None


def hash_arguments(arguments: dict[str, Any] | None) -> str:
    """A stable hash of a tool's arguments.

    Sorted keys and a compact separator, so the same call hashes the same way whichever side
    builds the dictionary and in whichever order. Values are serialised rather than stringified:
    `{"tail": 100}` and `{"tail": "100"}` are different calls and have to hash differently.
    """
    canonical = json.dumps(arguments or {}, sort_keys=True, separators=(",", ":"), default=str)

    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalTokens:
    """Issues and verifies approval tokens.

    Both halves live here even though only verification runs in this service: the issuing half is
    what the tests approve with, what `scripts/` uses to drive a demo, and the executable
    specification the backend's C# implementation is checked against. A token format described in
    prose in two languages is a token format that will diverge.
    """

    def __init__(self, secret: str | None, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._key = (secret or "").encode("utf-8")
        self._ttl = ttl_seconds

    @property
    def configured(self) -> bool:
        return bool(self._key)

    def issue(
        self,
        *,
        recommendation_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        approved_by: str | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """The token for one approved action.

        Raises:
            RuntimeError: no secret is configured. An unset secret must not quietly become a
                token everybody can mint, which is what returning an empty string would be.
        """
        if not self.configured:
            raise RuntimeError(
                "No approval secret is configured, so approvals cannot be issued. "
                "Set APPROVAL_SECRET."
            )

        grant = ApprovalGrant(
            recommendation_id=recommendation_id,
            tool=tool,
            arguments_hash=hash_arguments(arguments),
            expires_at=int(time.time()) + (ttl_seconds or self._ttl),
            approved_by=approved_by,
        )

        payload = _encode(
            {
                "v": SCHEME,
                "rec": grant.recommendation_id,
                "tool": grant.tool,
                "args": grant.arguments_hash,
                "exp": grant.expires_at,
                **({"by": grant.approved_by} if grant.approved_by else {}),
            }
        )

        return f"{payload}.{_sign(self._key, payload)}"

    def verify(
        self,
        token: str | None,
        *,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> ApprovalCheck:
        """Whether this token authorises *this* call.

        The tool and the arguments are the caller's, not the token's. A verifier that read them
        out of the token would answer "is this a token I issued" — which is true of every token
        this system has ever issued, including the one that approved something else.
        """
        if not self.configured:
            return ApprovalCheck(
                False,
                "No approval secret is configured, so no approval can be accepted.",
                ApprovalFailure.NOT_CONFIGURED,
            )

        if not token or token.count(".") != 1:
            return ApprovalCheck(
                False, "The approval token is not in the expected form.", ApprovalFailure.MALFORMED
            )

        payload, signature = token.split(".", 1)

        # Constant time: a comparison that returns early tells an attacker how much of a forged
        # signature was right, one byte at a time.
        if not hmac.compare_digest(signature, _sign(self._key, payload)):
            return ApprovalCheck(
                False, "The approval token's signature is not valid.", ApprovalFailure.BAD_SIGNATURE
            )

        try:
            claims = _decode(payload)
        except (ValueError, json.JSONDecodeError):
            return ApprovalCheck(
                False, "The approval token could not be read.", ApprovalFailure.MALFORMED
            )

        if claims.get("v") != SCHEME:
            return ApprovalCheck(
                False,
                f"The approval token uses scheme '{claims.get('v')}', which this service "
                f"does not accept.",
                ApprovalFailure.MALFORMED,
            )

        grant = ApprovalGrant(
            recommendation_id=str(claims.get("rec", "")),
            tool=str(claims.get("tool", "")),
            arguments_hash=str(claims.get("args", "")),
            expires_at=int(claims.get("exp", 0)),
            approved_by=claims.get("by"),
        )

        if grant.expired:
            return ApprovalCheck(
                False,
                "The approval has expired. An approval describes the system somebody was "
                "looking at; approve the action again.",
                ApprovalFailure.EXPIRED,
                grant,
            )

        if grant.tool != tool:
            # Named in the message. This is the case the whole scheme exists for, and a reader
            # who cannot see what was approved cannot tell an attack from a wiring mistake.
            return ApprovalCheck(
                False,
                f"The approval is for '{grant.tool}' and the call is to '{tool}'.",
                ApprovalFailure.WRONG_TOOL,
                grant,
            )

        if grant.arguments_hash != hash_arguments(arguments):
            return ApprovalCheck(
                False,
                f"The approval for '{tool}' was given for different arguments than the ones "
                "being called with.",
                ApprovalFailure.WRONG_ARGUMENTS,
                grant,
            )

        return ApprovalCheck(True, "Approved.", None, grant)


def _sign(key: bytes, payload: str) -> str:
    return _b64(hmac.new(key, payload.encode("utf-8"), hashlib.sha256).digest())


def _encode(claims: dict[str, Any]) -> str:
    return _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _decode(payload: str) -> dict[str, Any]:
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding)

    claims = json.loads(decoded)

    if not isinstance(claims, dict):
        raise ValueError("the payload is not an object")

    return claims


def _b64(raw: bytes) -> str:
    # URL-safe and unpadded: a token travels in a JSON body today and in a URL the moment
    # somebody writes a link that approves something.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def approval_tokens(config: Any) -> ApprovalTokens:
    """The issuer this service verifies with, from settings.

    A factory rather than two constructor calls: the HTTP surface and the investigation runner
    both build a policy, and a TTL configured in one of them and defaulted in the other is a
    difference nobody would notice until an approval expired early in one path.
    """
    return ApprovalTokens(
        config.approval_secret or None,
        ttl_seconds=getattr(config, "approval_ttl_seconds", DEFAULT_TTL_SECONDS),
    )
