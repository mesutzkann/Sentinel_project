"""Prompts as versioned files, not string literals scattered through the code.

Three reasons this is a registry rather than f-strings at the call sites:

* A prompt is the thing that changes when quality changes. Keeping each one in its own file
  means ``git log`` over ``llm/prompts/`` is a record of what was tried.
* Versions coexist. Phase 8 benchmarks a fine-tuned router against its base model, and Phase 11
  compares agent runs; both need to pin the exact prompt a run used rather than whatever the
  file says today.
* Rendering is checked. A prompt referencing a variable the caller did not pass fails loudly at
  render time, instead of reaching the model with an empty hole in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


class PromptNotFoundError(LookupError):
    """No prompt file for that name and version."""


class PromptRenderError(ValueError):
    """A placeholder had no matching value."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One prompt template.

    ``{{ name }}`` placeholders are substituted by :meth:`render`. Deliberately not
    :meth:`str.format`: prompts contain JSON examples full of braces, and every one of them
    would have to be escaped.
    """

    name: str
    version: str
    template: str

    @property
    def id(self) -> str:
        """What gets recorded against a run, e.g. ``root_cause.v1``."""
        return f"{self.name}.{self.version}"

    @property
    def variables(self) -> frozenset[str]:
        return frozenset(_PLACEHOLDER.findall(self.template))

    def render(self, **values: object) -> str:
        """Substitute every placeholder.

        Raises:
            PromptRenderError: a placeholder has no value. Silently leaving it would ship
                ``{{ evidence }}`` to the model, which reliably produces a confident answer
                about nothing.
        """
        missing = self.variables - values.keys()
        if missing:
            raise PromptRenderError(
                f"{self.id} needs {sorted(missing)}, which were not provided."
            )

        def substitute(match: re.Match[str]) -> str:
            return str(values[match.group(1)])

        return _PLACEHOLDER.sub(substitute, self.template)


class PromptRegistry:
    """Loads prompts from ``llm/prompts/<name>.<version>.md``."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or _PROMPT_DIR

    def get(self, name: str, version: str = "v1") -> Prompt:
        path = self._directory / f"{name}.{version}.md"

        if not path.is_file():
            raise PromptNotFoundError(
                f"No prompt '{name}.{version}' in {self._directory}. "
                f"Available: {sorted(p.id for p in self.all()) or 'none'}"
            )

        return Prompt(name=name, version=version, template=path.read_text(encoding="utf-8"))

    def all(self) -> list[Prompt]:
        """Every prompt on disk, for the registry listing and for tests that render them all."""
        prompts: list[Prompt] = []

        for path in sorted(self._directory.glob("*.*.md")):
            name, version = path.name.removesuffix(".md").rsplit(".", 1)
            prompts.append(
                Prompt(name=name, version=version, template=path.read_text(encoding="utf-8"))
            )

        return prompts


@lru_cache(maxsize=1)
def registry() -> PromptRegistry:
    """The process-wide registry. Cached because prompt files do not change at runtime."""
    return PromptRegistry()
