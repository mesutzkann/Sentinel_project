"""Metadata filter semantics.

These are the tests that keep the two implementations honest. The SQL side is exercised against
a real database in ``test_rag_store.py``; what is fixed here is what the answer is supposed to
be, so that a divergence shows up as a failing test rather than as a search that quietly returns
three results instead of five.
"""

from __future__ import annotations

import pytest

from rag.filters import FilterError, matches, normalize

CHUNK = {
    "document_type": "postmortem",
    "service": "orders",
    "external_id": "INC-00001",
    "severity": "high",
    "duration_minutes": "47",
}


def test_no_filter_matches_everything() -> None:
    assert normalize(None) == {}
    assert matches(CHUNK, {})


def test_a_scalar_must_be_equal() -> None:
    assert matches(CHUNK, normalize({"service": "orders"}))
    assert not matches(CHUNK, normalize({"service": "payments"}))


def test_every_key_must_match() -> None:
    assert matches(CHUNK, normalize({"service": "orders", "severity": "high"}))
    assert not matches(CHUNK, normalize({"service": "orders", "severity": "low"}))


def test_a_list_matches_any_of_its_values() -> None:
    assert matches(CHUNK, normalize({"service": ["payments", "orders"]}))
    assert not matches(CHUNK, normalize({"service": ["payments", "users"]}))


def test_a_key_the_chunk_does_not_carry_never_matches() -> None:
    # The deliberate reading: a document that never says which service it is about is not
    # evidence about orders.
    assert not matches(CHUNK, normalize({"incident_id": "INC-00001"}))


def test_a_number_and_its_string_are_the_same_filter() -> None:
    # A filter arriving over HTTP has no types. Both sides are compared as text so that 47 and
    # "47" cannot mean different things depending on how the request was written.
    assert matches(CHUNK, normalize({"duration_minutes": 47}))
    assert matches(CHUNK, normalize({"duration_minutes": "47"}))


def test_a_boolean_is_spelled_the_way_json_spells_it() -> None:
    assert normalize({"resolved": True}) == {"resolved": "true"}
    assert matches({"resolved": "true"}, normalize({"resolved": True}))


def test_duplicate_values_collapse_and_keep_their_order() -> None:
    assert normalize({"service": ["orders", "orders", "payments"]}) == {
        "service": ["orders", "payments"]
    }


def test_a_single_value_list_normalizes_to_a_scalar() -> None:
    assert normalize({"service": ["orders"]}) == {"service": "orders"}


def test_an_empty_any_of_is_rejected() -> None:
    # It would match nothing, which is indistinguishable in the results from a corpus that has
    # nothing to say.
    with pytest.raises(FilterError, match="no values"):
        normalize({"service": []})


def test_a_null_is_rejected_rather_than_ignored() -> None:
    with pytest.raises(FilterError, match="null"):
        normalize({"service": None})


def test_a_nested_filter_is_rejected() -> None:
    with pytest.raises(FilterError, match="scalar"):
        normalize({"service": {"eq": "orders"}})

    with pytest.raises(FilterError, match="scalars"):
        normalize({"service": [["orders"]]})
