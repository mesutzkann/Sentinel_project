"""source-code-mcp: reading the code the stack trace points at.

A stack trace naming `PaymentProcessor.cs:66` is only half an answer. This server supplies the
other half — the line itself, the guard that is missing from it, and where the symbol is used
elsewhere. Five of the fifteen chaos scenarios require reading source to distinguish.

Every path is resolved against the mount root and rejected if it escapes. The arguments here
are written by a language model, so path traversal is a live concern rather than a theoretical
one.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from _shared.config import settings
from _shared.results import empty, result, truncated
from _shared.server import build_server, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "source-code-mcp",
    instructions=(
        "Read-only access to the repository's source. Use it after a stack trace or a metric "
        "has narrowed the problem to a file: read the code, find where a symbol is defined and "
        "used, and confirm the defect rather than inferring it."
    ),
)

# Source only. Binaries and dependency trees are noise the agent cannot use and would blow the
# context window apart.
_SOURCE_SUFFIXES = {
    ".cs", ".py", ".ts", ".tsx", ".js", ".jsx", ".sql", ".json", ".yml", ".yaml",
    ".md", ".toml", ".sh", ".ps1", ".csproj", ".sln", ".props", ".targets",
}

_IGNORED_DIRECTORIES = {
    "bin", "obj", "node_modules", ".git", ".venv", "venv", "dist", "__pycache__",
    ".pytest_cache", ".ruff_cache", "TestResults",
}

_MAX_FILE_BYTES = 512_000


class PathOutsideRepositoryError(ValueError):
    """A path resolved outside the mounted repository."""


def _resolve(relative: str) -> Path:
    """Resolves a repository-relative path, refusing anything that escapes the mount.

    ``Path.resolve`` collapses ``..`` before the check, so a traversal cannot survive it. The
    check is on the resolved path rather than on the string, because ``a/../../etc/passwd``
    contains nothing suspicious to look for textually.
    """
    root = Path(settings().repo_root).resolve()
    candidate = (root / relative).resolve()

    if not candidate.is_relative_to(root):
        raise PathOutsideRepositoryError(f"'{relative}' resolves outside the repository.")

    return candidate


def _is_source(path: Path) -> bool:
    if path.suffix.lower() not in _SOURCE_SUFFIXES:
        return False

    return not any(part in _IGNORED_DIRECTORIES for part in path.parts)


def _walk(root: Path) -> list[Path]:
    """Every source file under ``root``, skipping directories that cannot contain any.

    Uses os.walk and prunes in place rather than filtering the output of rglob. The difference
    is not cosmetic: rglob descends into every directory before anything filters it, so a
    repository with two virtualenvs and a node_modules costs tens of seconds per call — long
    enough to make the tool useless inside an investigation. Pruning the directory list stops
    the walk from entering them at all.
    """
    found: list[Path] = []

    for directory, subdirectories, filenames in os.walk(root):
        # Assigning to the slice is what prunes: os.walk reads this list back to decide where to
        # descend next, so replacing its contents removes those branches from the walk.
        subdirectories[:] = [d for d in subdirectories if d not in _IGNORED_DIRECTORIES]

        for filename in filenames:
            path = Path(directory) / filename

            if path.suffix.lower() in _SOURCE_SUFFIXES:
                found.append(path)

    return found


def _relative(path: Path) -> str:
    return path.relative_to(Path(settings().repo_root).resolve()).as_posix()


# --------------------------------------------------------------------------- tools ----


@read_only(
    server,
    "Search the source for a string. Returns each match with the lines around it, so a hit is "
    "readable without a second call. Case-insensitive.",
)
async def search_code(
    query: str,
    path_contains: str | None = None,
    limit: int = 30,
    context_lines: int = 2,
) -> dict[str, Any]:
    """Args:
    query: Text or regular expression to find.
    path_contains: Only search files whose path contains this, e.g. `payments`.
    limit: Maximum matches to return.
    context_lines: Lines of context on each side of a match.
    """
    root = Path(settings().repo_root).resolve()

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        return {"error": f"'{query}' is not a valid regular expression: {exc}"}

    matches: list[dict[str, Any]] = []

    for file in _walk(root):
        if path_contains and path_contains.lower() not in _relative(file).lower():
            continue

        if file.stat().st_size > _MAX_FILE_BYTES:
            continue

        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for number, line in enumerate(lines, start=1):
            if not pattern.search(line):
                continue

            start = max(0, number - 1 - context_lines)
            end = min(len(lines), number + context_lines)

            matches.append(
                {
                    "path": _relative(file),
                    "line": number,
                    "match": line.strip()[:300],
                    "context": "\n".join(lines[start:end]),
                }
            )

            if len(matches) >= limit * 4:
                break

        if len(matches) >= limit * 4:
            break

    if not matches:
        return empty(
            "the repository",
            f"source files for '{query}'"
            + (f" under paths containing '{path_contains}'" if path_contains else ""),
            query=query,
        )

    shown, was_truncated = truncated(matches, min(limit, settings().max_results))

    return result(
        found=len(matches),
        window="the repository",
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        query=query,
    )


@read_only(
    server,
    "Read a file, or a line range of one. Pass the range from a stack trace to see the failing "
    "line with its surroundings instead of the whole file.",
)
async def read_file(path: str, start: int | None = None, end: int | None = None) -> dict[str, Any]:
    """Args:
    path: Repository-relative path, e.g. `sample-services/payments/PaymentProcessor.cs`.
    start: First line, 1-based. Omit to start at the beginning.
    end: Last line, inclusive. Omit to read to the end.
    """
    try:
        file = _resolve(path)
    except PathOutsideRepositoryError as exc:
        return {"error": str(exc)}

    if not file.is_file():
        return {"error": f"No file at '{path}'."}

    if file.stat().st_size > _MAX_FILE_BYTES:
        return {"error": f"'{path}' is larger than {_MAX_FILE_BYTES} bytes. Read a range."}

    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()

    first = max(1, start or 1)
    last = min(len(lines), end or len(lines))

    if first > len(lines):
        return {"error": f"'{path}' has {len(lines)} lines; {first} is past the end."}

    selected = lines[first - 1 : last]

    return {
        "path": path,
        "total_lines": len(lines),
        "start": first,
        "end": last,
        # Numbered, so a model quoting a line quotes its real number rather than counting from
        # the top of the excerpt.
        "content": "\n".join(f"{first + i:5d}  {line}" for i, line in enumerate(selected)),
    }


# Definition sites for the languages in this repository. Deliberately not a parser: a regular
# expression that finds a C# class or a Python function is enough to locate a symbol, and the
# result is verified by reading the file rather than trusted on its own.
_DEFINITION_PATTERNS = (
    r"\b(?:class|record|struct|interface|enum)\s+{name}\b",
    r"\b(?:public|private|protected|internal|static|async|sealed|override|virtual)"
    r"[\w<>,\[\]\s]*\s{name}\s*\(",
    r"\bdef\s+{name}\s*\(",
    r"\b(?:const|let|var|function)\s+{name}\b",
)


@read_only(
    server,
    "Find where a symbol is defined — a class, method, function or type. Returns the file and "
    "line, which read_file can then open.",
)
async def find_symbol(name: str, limit: int = 20) -> dict[str, Any]:
    """Args:
    name: Symbol name, exact and case-sensitive, e.g. `PaymentProcessor`.
    limit: Maximum definitions to return.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return {"error": "A symbol name must be a plain identifier."}

    patterns = [re.compile(p.format(name=re.escape(name))) for p in _DEFINITION_PATTERNS]
    root = Path(settings().repo_root).resolve()
    found: list[dict[str, Any]] = []

    for file in _walk(root):
        if file.stat().st_size > _MAX_FILE_BYTES:
            continue

        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for number, line in enumerate(lines, start=1):
            if any(p.search(line) for p in patterns):
                found.append(
                    {"path": _relative(file), "line": number, "definition": line.strip()[:300]}
                )

    if not found:
        return empty(
            "the repository",
            f"definitions of '{name}'",
            symbol=name,
            hint="If the symbol is only used and never defined here, try find_references.",
        )

    shown, was_truncated = truncated(found, limit)

    return result(
        found=len(found),
        window="the repository",
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        symbol=name,
    )


@read_only(
    server,
    "Every place a symbol appears, definitions included. Use it to judge blast radius: how many "
    "call sites a change would touch, and which services use the thing that broke.",
)
async def find_references(symbol: str, limit: int = 40) -> dict[str, Any]:
    """Args:
    symbol: Symbol name, exact and case-sensitive.
    limit: Maximum references to return.
    """
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", symbol):
        return {"error": "A symbol name must be a plain identifier."}

    # Word-bounded, so searching for `Order` does not match `OrderItem` or `Reorder`.
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    root = Path(settings().repo_root).resolve()

    references: list[dict[str, Any]] = []
    per_file: dict[str, int] = {}

    for file in _walk(root):
        if file.stat().st_size > _MAX_FILE_BYTES:
            continue

        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        for number, line in enumerate(lines, start=1):
            if pattern.search(line):
                relative = _relative(file)
                per_file[relative] = per_file.get(relative, 0) + 1
                references.append(
                    {"path": relative, "line": number, "text": line.strip()[:300]}
                )

    if not references:
        return empty("the repository", f"references to '{symbol}'", symbol=symbol)

    shown, was_truncated = truncated(references, min(limit, settings().max_results))

    return result(
        found=len(references),
        window="the repository",
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        symbol=symbol,
        files=dict(sorted(per_file.items(), key=lambda kv: kv[1], reverse=True)[:20]),
    )


@read_only(
    server,
    "The shape of a directory: source files and their sizes, without build output or "
    "dependencies. Use it to orient before searching when you do not know the layout.",
)
async def get_project_structure(path: str = "", max_depth: int = 3) -> dict[str, Any]:
    """Args:
    path: Repository-relative directory. Omit for the repository root.
    max_depth: How many levels below `path` to descend.
    """
    try:
        directory = _resolve(path) if path else Path(settings().repo_root).resolve()
    except PathOutsideRepositoryError as exc:
        return {"error": str(exc)}

    if not directory.is_dir():
        return {"error": f"'{path}' is not a directory."}

    root = Path(settings().repo_root).resolve()
    base_depth = len(directory.relative_to(root).parts)

    entries: list[dict[str, Any]] = []

    for file in sorted(_walk(directory)):
        relative = file.relative_to(root)

        if len(relative.parts) - base_depth > max_depth:
            continue

        entries.append(
            {"path": relative.as_posix(), "size_bytes": file.stat().st_size}
        )

    if not entries:
        return empty("the repository", f"source files under '{path or '.'}'", path=path)

    shown, was_truncated = truncated(entries, settings().max_results)

    return result(
        found=len(entries),
        window="the repository",
        items=shown,
        truncated_at=len(shown) if was_truncated else None,
        path=path or ".",
    )


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
