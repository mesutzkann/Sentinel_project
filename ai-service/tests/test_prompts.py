"""The prompt registry: loading, rendering, and refusing to render a hole."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm.prompts import (
    Prompt,
    PromptNotFoundError,
    PromptRegistry,
    PromptRenderError,
    registry,
)


def test_shipped_prompts_load() -> None:
    prompts = registry().all()

    ids = {p.id for p in prompts}
    assert "structured_extraction.v1" in ids
    assert "incident_summary.v1" in ids


def test_every_shipped_prompt_declares_a_schema_placeholder() -> None:
    """A structured-output prompt without the schema in it tells the model nothing about shape."""
    for prompt in registry().all():
        assert "schema" in prompt.variables, f"{prompt.id} does not reference the schema"


def test_render_substitutes_placeholders() -> None:
    prompt = Prompt(name="t", version="v1", template="A {{ x }} and {{ y }}.")

    assert prompt.render(x="one", y="two") == "A one and two."


def test_render_refuses_a_missing_variable() -> None:
    """Leaving the placeholder in would ship `{{ evidence }}` to the model, which answers anyway."""
    prompt = Prompt(name="t", version="v1", template="Evidence: {{ evidence }}")

    with pytest.raises(PromptRenderError, match="evidence"):
        prompt.render()


def test_braces_in_json_examples_survive_rendering() -> None:
    """Prompts are full of JSON, which is why this is not str.format."""
    prompt = Prompt(name="t", version="v1", template='Example: {"a": 1}. Input: {{ input }}')

    assert prompt.render(input="x") == 'Example: {"a": 1}. Input: x'


def test_unknown_prompt_names_what_is_available() -> None:
    with pytest.raises(PromptNotFoundError, match="Available"):
        registry().get("does_not_exist")


def test_versions_coexist(tmp_path: Path) -> None:
    """Phase 8 and 11 pin the exact prompt a run used, so two versions must load side by side."""
    (tmp_path / "router.v1.md").write_text("one {{ x }}", encoding="utf-8")
    (tmp_path / "router.v2.md").write_text("two {{ x }}", encoding="utf-8")

    local = PromptRegistry(tmp_path)

    assert local.get("router", "v1").render(x="!") == "one !"
    assert local.get("router", "v2").render(x="!") == "two !"
    assert {p.id for p in local.all()} == {"router.v1", "router.v2"}
