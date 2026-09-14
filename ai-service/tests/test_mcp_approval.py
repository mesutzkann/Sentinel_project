"""The approval token: what a person's click authorises, and what it does not.

This is the security boundary of Phase 10, so the tests are written as the attacks they refuse.
The one that matters most is the second: a token that proves only "a human approved something"
is a token that authorises everything a human has ever approved, and the Phase 4 placeholder was
exactly that.
"""

from __future__ import annotations

import time

from mcp_client.approval import (
    DEFAULT_TTL_SECONDS,
    ApprovalFailure,
    ApprovalTokens,
    hash_arguments,
)

SECRET = "dev-only-approval-secret"
RESTART = "docker-mcp/restart_container"


def tokens(ttl: int = DEFAULT_TTL_SECONDS) -> ApprovalTokens:
    return ApprovalTokens(SECRET, ttl_seconds=ttl)


def approve(issuer: ApprovalTokens | None = None, **overrides) -> str:
    issuer = issuer or tokens()
    fields = {
        "recommendation_id": "rec-42",
        "tool": RESTART,
        "arguments": {"name": "sentinel-orders"},
        "approved_by": "operator@sentinel",
        **overrides,
    }

    return issuer.issue(**fields)


def test_an_approval_authorises_the_call_it_was_given_for() -> None:
    issuer = tokens()
    check = issuer.verify(approve(issuer), tool=RESTART, arguments={"name": "sentinel-orders"})

    assert check.valid
    assert check.grant is not None
    assert check.grant.recommendation_id == "rec-42"
    assert check.grant.approved_by == "operator@sentinel"


def test_an_approval_for_one_tool_does_not_authorise_another() -> None:
    """The failure the Phase 4 placeholder had: "somebody approved something" is not authority."""
    issuer = tokens()
    check = issuer.verify(
        approve(issuer),
        tool="database-mcp/execute_write_query",
        arguments={"name": "sentinel-orders"},
    )

    assert not check.valid
    assert check.failure is ApprovalFailure.WRONG_TOOL

    # The reason names both, because a reader has to tell an attack from a wiring mistake.
    assert RESTART in check.reason
    assert "database-mcp/execute_write_query" in check.reason


def test_an_approval_for_one_container_does_not_restart_another() -> None:
    issuer = tokens()
    check = issuer.verify(approve(issuer), tool=RESTART, arguments={"name": "sentinel-payments"})

    assert not check.valid
    assert check.failure is ApprovalFailure.WRONG_ARGUMENTS


def test_adding_an_argument_after_approval_invalidates_it() -> None:
    """An approved `restart(name)` must not become `restart(name, force=True)` in flight."""
    issuer = tokens()
    check = issuer.verify(
        approve(issuer),
        tool=RESTART,
        arguments={"name": "sentinel-orders", "force": True},
    )

    assert not check.valid
    assert check.failure is ApprovalFailure.WRONG_ARGUMENTS


def test_argument_order_is_not_part_of_the_call() -> None:
    """The two sides build the dictionary independently; key order is not a difference."""
    issuer = tokens()
    token = issuer.issue(
        recommendation_id="rec-42",
        tool=RESTART,
        arguments={"name": "orders", "timeout": 30},
    )

    assert issuer.verify(token, tool=RESTART, arguments={"timeout": 30, "name": "orders"}).valid


def test_a_number_and_the_string_of_it_are_different_calls() -> None:
    assert hash_arguments({"tail": 100}) != hash_arguments({"tail": "100"})


def test_an_expired_approval_is_refused() -> None:
    """An approval describes the system somebody was looking at, and that goes stale."""
    issuer = tokens(ttl=-1)
    check = issuer.verify(approve(issuer), tool=RESTART, arguments={"name": "sentinel-orders"})

    assert not check.valid
    assert check.failure is ApprovalFailure.EXPIRED
    assert "expired" in check.reason.casefold()


def test_the_default_window_is_minutes_not_hours() -> None:
    """Revocation is what a derived token trades away, so the window is the whole mitigation."""
    issued_at = int(time.time())
    check = tokens().verify(approve(), tool=RESTART, arguments={"name": "sentinel-orders"})

    assert check.grant is not None
    assert 0 < check.grant.expires_at - issued_at <= 900


def test_a_forged_signature_is_refused() -> None:
    payload, _ = approve().split(".", 1)
    forged = f"{payload}.{'A' * 43}"

    assert tokens().verify(forged, tool=RESTART).failure is ApprovalFailure.BAD_SIGNATURE


def test_a_token_from_another_installation_is_refused() -> None:
    """The secret is what makes an approval this system's, and not somebody else's."""
    elsewhere = ApprovalTokens("a-different-secret")
    check = tokens().verify(
        approve(elsewhere), tool=RESTART, arguments={"name": "sentinel-orders"}
    )

    assert check.failure is ApprovalFailure.BAD_SIGNATURE


def test_an_edited_payload_is_refused() -> None:
    """The claims are signed, so raising the expiry or swapping the tool breaks the signature."""
    tampered = ApprovalTokens(SECRET).issue(
        recommendation_id="rec-42", tool="docker-mcp/get_container_logs"
    )
    _, signature = tampered.split(".", 1)
    other, _ = approve().split(".", 1)

    assert tokens().verify(f"{other}.{signature}", tool=RESTART).failure is (
        ApprovalFailure.BAD_SIGNATURE
    )


def test_nonsense_is_refused_as_malformed_rather_than_crashing() -> None:
    issuer = tokens()

    for candidate in ("", None, "no-dot", "a.b.c", "not-base64.$$$$"):
        check = issuer.verify(candidate, tool=RESTART)

        assert not check.valid
        assert check.failure in (ApprovalFailure.MALFORMED, ApprovalFailure.BAD_SIGNATURE)


def test_without_a_secret_nothing_can_be_approved_or_issued() -> None:
    """The closed default: an unset secret is not a token everybody can mint."""
    unconfigured = ApprovalTokens(None)

    assert not unconfigured.configured
    assert unconfigured.verify(approve(), tool=RESTART).failure is ApprovalFailure.NOT_CONFIGURED

    try:
        unconfigured.issue(recommendation_id="rec-42", tool=RESTART)
        raised = False
    except RuntimeError:
        raised = True

    assert raised, "issuing without a secret must fail loudly rather than return something"


def test_the_arguments_are_not_carried_in_the_token() -> None:
    """A token travels in logs and in URLs; a patch or a connection string must not travel in it."""
    token = approve(arguments={"connection_string": "Host=db;Password=hunter2"})

    assert "hunter2" not in token
    assert "Password" not in token
