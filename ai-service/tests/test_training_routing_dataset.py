"""The routing dataset, checked for the properties that make a benchmark mean anything.

Two of these are worth more than the rest. If one phrasing can appear in both train and test,
the accuracy measured on test is memorisation; and if the labels can drift from
`routing/plans.py`, the model is being trained to disagree with the planner that will act on it.
"""

from __future__ import annotations

import re
from dataclasses import asdict

import pytest

from routing.plans import decide
from routing.schema import Intent
from training.routing_dataset import (
    DEFAULT_VARIANTS,
    SPLITS,
    Example,
    add_typos,
    append_paraphrase_cache,
    choose_seeds,
    expand,
    normalise,
    read_paraphrase_cache,
    split,
    validate,
)
from training.templates import ALL_TEMPLATES, SERVICES


@pytest.fixture(scope="module")
def examples():
    return expand(DEFAULT_VARIANTS)


@pytest.fixture(scope="module")
def splits(examples):
    return split(examples + add_typos(examples, 0.1))


def test_every_row_is_a_label_the_router_would_accept(examples) -> None:
    validate(examples)

    assert len(examples) > 1000, "the templates should produce a four-figure dataset"


def test_labels_come_from_the_plan_table(examples) -> None:
    """The flags and the tools are not written by hand anywhere — they follow from the intent."""
    for example in examples:
        expected = decide(Intent(example.intent), example.target_service)

        assert example.decision() == expected


def test_no_query_appears_twice(examples) -> None:
    keys = [normalise(example.query) for example in examples]

    assert len(keys) == len(set(keys))


def test_a_phrasing_never_lands_on_both_sides_of_the_measurement(splits) -> None:
    """The property the whole split is built around.

    Two fills of one template are the same question with a different service in it. Letting one
    into train and its twin into test would measure whether the model remembers the phrasing.
    """
    by_split = {name: {row.template for row in rows} for name, rows in splits.items()}

    assert not by_split["train"] & by_split["test"]
    assert not by_split["train"] & by_split["val"]
    assert not by_split["val"] & by_split["test"]


def test_every_intent_can_be_measured(splits) -> None:
    """An intent with no test row is an intent the benchmark cannot say anything about."""
    for name, _ in SPLITS:
        present = {row.intent for row in splits[name]}

        assert present == {intent.value for intent in Intent}, f"{name} is missing an intent"


def test_the_split_is_roughly_the_shares_it_claims(splits) -> None:
    total = sum(len(rows) for rows in splits.values())

    # Whole phrasings are indivisible, so this lands near the shares rather than on them.
    assert 0.74 <= len(splits["train"]) / total <= 0.86
    assert 0.05 <= len(splits["val"]) / total <= 0.16
    assert 0.05 <= len(splits["test"]) / total <= 0.16


def test_generation_is_deterministic() -> None:
    """A dataset that changed under a rerun would make every benchmark number incomparable."""
    first = expand(DEFAULT_VARIANTS)
    second = expand(DEFAULT_VARIANTS)

    assert [row.query for row in first] == [row.query for row in second]


def test_a_typo_keeps_the_label_and_changes_the_query(examples) -> None:
    typos = add_typos(examples, 0.1)

    assert typos

    by_id = {example.query: example for example in examples}

    for mangled in typos:
        assert mangled.query not in by_id, "a typo that changed nothing is not an example"
        assert mangled.source == "typo"
        assert mangled.decision() == decide(Intent(mangled.intent), mangled.target_service)


def test_turkish_suffixes_survive_the_service_name(examples) -> None:
    """`{service}'ta` may only take the bare name.

    Substituting a longer form into a suffixed slot produces "orders servisi'ta", which is not
    Turkish and would teach the model that it is.
    """
    for example in examples:
        assert not re.search(r"servisi'", example.query), example.query


def test_a_service_is_named_the_ways_people_name_it(examples) -> None:
    queries = [example.query for example in examples]

    assert any("the orders service" in query for query in queries)
    assert any("orders servisi" in query for query in queries)
    assert any(re.search(r"\borders\b(?! servis)", query) for query in queries)


def test_a_query_that_names_nobody_has_no_target_service(examples) -> None:
    """"Draw me the call graph" names no service, and null is the label that says so."""
    nameless = [example for example in examples if example.target_service is None]

    assert nameless

    for example in nameless:
        assert not any(service in example.query.split() for service in SERVICES)


def test_normalise_folds_the_turkish_characters() -> None:
    assert normalise("Neden yavaş?") == normalise("neden yavas")
    assert normalise("ORDERS") == normalise("orders")


def test_the_templates_cover_every_intent_in_both_languages() -> None:
    by_intent: dict[Intent, set[str]] = {}

    for template in ALL_TEMPLATES:
        by_intent.setdefault(template.intent, set()).add(template.language)

    for intent in Intent:
        languages = by_intent.get(intent, set())

        assert "en" in languages, f"{intent} has no English phrasing"
        assert "tr" in languages, f"{intent} has no Turkish phrasing"


def test_every_intent_has_enough_phrasings_per_language_to_reach_training() -> None:
    """The invariant behind Phase 8's worst number.

    The split reserves one phrasing per intent for validation and two for test. With a single
    mixed-language phrasing per intent, several intents had that one phrasing reserved away and
    the model saw no mixed question for them at all in training — it scored 0.11 on mixed against
    0.90 on English. Four per language per intent is the floor that makes that impossible.
    """
    counts: dict[tuple[Intent, str], int] = {}

    for template in ALL_TEMPLATES:
        counts[(template.intent, template.language)] = (
            counts.get((template.intent, template.language), 0) + 1
        )

    for intent in Intent:
        for language in ("en", "tr", "mixed"):
            found = counts.get((intent, language), 0)

            assert found >= 4, f"{intent.value}/{language} has {found} phrasings, needs 4"


def test_the_weak_groups_get_most_of_the_paraphrasing(examples) -> None:
    """Seeds are drawn where the router failed, not uniformly across the dataset."""
    seeds = choose_seeds(examples, 300)
    share = sum(1 for seed in seeds if seed.language == "mixed") / len(seeds)
    everywhere = sum(1 for row in examples if row.language == "mixed") / len(examples)

    assert share > everywhere * 2
    assert len(seeds) == 300
    assert len({seed.id for seed in seeds}) == 300, "a seed must not be drawn twice"


def test_a_resumed_paraphrase_run_does_not_ask_twice(tmp_path, examples) -> None:
    """The cache is keyed by seed and model, and records the seeds that yielded nothing."""
    cache = tmp_path / "paraphrases.jsonl"
    seed = examples[0]
    rewrite = Example(**{**asdict(seed), "query": "something else entirely", "source": "paraphrase"})

    append_paraphrase_cache(cache, "qwen2.5:3b-instruct", seed, [rewrite])
    append_paraphrase_cache(cache, "qwen2.5:3b-instruct", examples[1], [])

    done, rows = read_paraphrase_cache(cache, "qwen2.5:3b-instruct")

    assert done == {seed.id, examples[1].id}
    assert [row.query for row in rows] == ["something else entirely"]
    assert rows[0].intent == seed.intent

    assert read_paraphrase_cache(cache, "qwen2.5:7b-instruct") == (set(), [])
