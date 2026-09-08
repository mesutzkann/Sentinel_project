"""Shaping what a tool returns.

Tool output goes straight into an LLM prompt, which constrains it in two ways a normal API
response is not constrained:

* Size. An unbounded result is a context-window failure, not a thorough answer. Everything is
  capped and says so when it truncates, because a silently shortened list reads to the model as
  a complete one.
* Empty results. "No errors in the last 15 minutes" is evidence — six of the fifteen chaos
  scenarios are recognised partly by the absence of error logs. A tool that answers ``[]`` lets
  the model read absence as failure-to-look, so every tool says what it searched and found
  nothing in.
"""

from __future__ import annotations

from typing import Any


def truncated(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    """Caps a list, reporting whether anything was dropped."""
    if len(items) <= limit:
        return items, False

    return items[:limit], True


def result(
    *,
    found: int,
    window: str,
    items: list[Any] | None = None,
    truncated_at: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The envelope every list-shaped tool returns.

    ``found`` and ``window`` are always present, so a model reading the response can tell "I
    looked at fifteen minutes and there was nothing" from "the tool failed".
    """
    payload: dict[str, Any] = {"found": found, "window": window}

    if items is not None:
        payload["items"] = items

    if truncated_at is not None:
        payload["truncated"] = True
        payload["truncated_at"] = truncated_at
        payload["note"] = (
            f"Only the first {truncated_at} of {found} results are shown. "
            "Narrow the time window or the filter to see the rest."
        )

    payload.update(extra)
    return payload


def empty(window: str, searched: str, **extra: Any) -> dict[str, Any]:
    """A deliberate nothing.

    Spelled out rather than returned as an empty list: absence of a signal is evidence in this
    system, and it has to be legible as such to the model reading it.
    """
    return {
        "found": 0,
        "window": window,
        "items": [],
        "note": f"Nothing matched. Searched {searched}. This is a result, not a failure.",
        **extra,
    }


def window_label(minutes: int) -> str:
    """Human-readable window, for the ``window`` field."""
    if minutes < 60:
        return f"last {minutes}m"

    hours = minutes / 60
    if hours.is_integer():
        return f"last {int(hours)}h"

    return f"last {hours:.1f}h"
