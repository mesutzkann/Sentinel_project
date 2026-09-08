"""Metadata filters, defined once so both halves of retrieval agree on what they mean.

The lexical index filters in Python over a list of dictionaries; the vector store filters in SQL
over a JSONB column. Two implementations of "service is orders" that disagree at the edges — on
a missing key, on a list value, on ``null`` — produce a hybrid result that is neither, and the
disagreement shows up as a slightly short result set rather than as an error. So the semantics
live here, in one paragraph, and both sides are tested against it.

A filter is a mapping of metadata key to either

* a scalar, which must be equal to the chunk's value, or
* a list, which the chunk's value must be one of.

Every key must match; there is no OR across keys. A key the chunk does not carry never matches,
which is the deliberate reading of "documents about orders": a document that never says which
service it is about is not evidence about orders.

Comparison is on text. Chunk metadata is written as text by
:meth:`rag.documents.Document.chunk_metadata` for exactly this reason, and filter values are
coerced to match, so a severity of ``3`` and a severity of ``"3"`` are the same filter whichever
side of the HTTP boundary they arrived from.
"""

from __future__ import annotations

from typing import Any

# Metadata keys retrieval is expected to filter on, from docs/planning.md. Not enforced —
# ingestion can attach anything and filtering on it works — but a typo in one of these is the
# realistic failure, and an endpoint that lists them lets a caller find out without reading the
# corpus.
KNOWN_KEYS = (
    "document_type",
    "service",
    "external_id",
    "incident_id",
    "scenario",
    "severity",
    "date",
    "path",
)


class FilterError(ValueError):
    """A filter that cannot be honoured as written."""


def normalize(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Validate a caller-supplied filter and put it in canonical form.

    Rejects rather than ignores what it cannot apply. A filter that is silently dropped returns
    more results than asked for, and "why is a payments runbook in my orders search" is a much
    harder question than a 422.
    """
    if not raw:
        return {}

    normalized: dict[str, Any] = {}

    for key, value in raw.items():
        if not key:
            raise FilterError("A filter key cannot be empty.")

        if isinstance(value, list | tuple | set):
            values = [v for v in value if v is not None]

            if not values:
                # An empty any-of matches nothing, which is almost never what was meant and is
                # indistinguishable in the results from a corpus that has nothing to say.
                raise FilterError(f"Filter '{key}' has no values to match.")

            if any(isinstance(v, dict | list) for v in values):
                raise FilterError(f"Filter '{key}' may only contain scalars.")

            texts = _dedupe([as_text(v) for v in values])
            normalized[key] = texts if len(texts) > 1 else texts[0]
            continue

        if isinstance(value, dict):
            raise FilterError(f"Filter '{key}' must be a scalar or a list of scalars.")

        if value is None:
            raise FilterError(f"Filter '{key}' is null. Omit the key instead.")

        normalized[key] = as_text(value)

    return normalized


def matches(metadata: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Whether one chunk's metadata satisfies every clause of a filter.

    Takes filters as :func:`normalize` leaves them. Metadata values are compared in text form so
    that a chunk stored before the text contract existed — or written by hand in a test — is
    matched on what it says rather than on how it is typed.
    """
    for key, expected in filters.items():
        if key not in metadata:
            return False

        actual = as_text(metadata[key])
        candidates = expected if isinstance(expected, list) else [expected]

        if actual not in candidates:
            return False

    return True


def as_text(value: Any) -> str:
    """A filter value in the text form the metadata column stores.

    The same coercion :meth:`rag.documents.Document.chunk_metadata` applies on the way in, kept
    identical on purpose: these two functions are the whole contract, and they only hold if they
    spell a boolean the same way.
    """
    if isinstance(value, bool):
        return "true" if value else "false"

    return str(value)


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving, so an any-of filter reads back the way it was written."""
    return list(dict.fromkeys(values))
