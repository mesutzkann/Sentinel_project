"""git-mcp: what changed, and when.

Correlation with time is the whole value here. Two of the fifteen chaos scenarios are
recognised primarily by a commit whose timestamp matches the moment errors began — a signal no
amount of log reading produces, because the log says what broke and never says what changed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from git import GitCommandError, InvalidGitRepositoryError, Repo

from _shared.config import settings
from _shared.results import empty, result, truncated
from _shared.server import build_server, read_only, serve

logger = logging.getLogger(__name__)

server = build_server(
    "git-mcp",
    instructions=(
        "History of the repository the sample services are built from. Use this to correlate a "
        "failure with a change: when errors begin abruptly at a single timestamp rather than "
        "rising with load, the cause is usually something that was deployed, and the commit "
        "that did it is here."
    ),
)


class RepositoryError(RuntimeError):
    """The repository could not be read."""


def _repo(repo: str | None = None) -> Repo:
    """Opens the mounted repository.

    ``repo`` is accepted for forward compatibility with a multi-repository setup, but is
    validated against the mount root: a path argument that can escape the mount is a file
    disclosure bug, and this server is called with arguments a language model wrote.
    """
    root = Path(settings().repo_root).resolve()

    if repo:
        candidate = (root / repo).resolve()

        if not candidate.is_relative_to(root):
            raise RepositoryError(f"'{repo}' resolves outside the repository root.")
    else:
        candidate = root

    try:
        return Repo(candidate, search_parent_directories=False)
    except (InvalidGitRepositoryError, OSError) as exc:
        raise RepositoryError(f"No git repository at {candidate}: {exc}") from exc


def _describe(commit: Any) -> dict[str, Any]:
    return {
        "sha": commit.hexsha,
        "short_sha": commit.hexsha[:8],
        "author": f"{commit.author.name}",
        "committed_at": datetime.fromtimestamp(commit.committed_date, tz=UTC).isoformat(),
        # First line only. A commit body can be long, and the subject is what a correlation
        # scan is reading.
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "files_changed": len(commit.stats.files),
    }


# --------------------------------------------------------------------------- tools ----


@read_only(
    server,
    "Recent commits, newest first. Narrow with `since_minutes` to the window around when a "
    "failure started: a commit inside that window is a candidate cause, and one outside it "
    "usually is not.",
)
async def get_recent_commits(
    repo: str | None = None,
    since_minutes: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Args:
    repo: Repository path relative to the mount root. Omit for the root repository.
    since_minutes: Only commits from the last N minutes.
    limit: Maximum commits to return.
    """
    repository = _repo(repo)
    capped = min(limit, settings().max_results)

    kwargs: dict[str, Any] = {"max_count": capped}
    window = "all history"

    if since_minutes is not None:
        after = datetime.now(tz=UTC) - timedelta(minutes=since_minutes)
        kwargs["after"] = after.isoformat()
        window = f"last {since_minutes}m"

    commits = [_describe(c) for c in repository.iter_commits(**kwargs)]

    if not commits:
        return empty(
            window,
            "the repository history",
            interpretation=(
                "No commits in this window. A failure that began here was not caused by a "
                "deployment of this repository."
            ),
        )

    return result(found=len(commits), window=window, items=commits)


@read_only(
    server,
    "The full diff of one commit. Read this once a commit is a suspect — the change itself is "
    "what confirms or rules it out.",
)
async def get_commit_diff(
    sha: str,
    repo: str | None = None,
    max_lines: int = 400,
) -> dict[str, Any]:
    """Args:
    sha: Commit hash, full or abbreviated.
    repo: Repository path relative to the mount root.
    max_lines: Cap on diff lines returned.
    """
    repository = _repo(repo)

    try:
        commit = repository.commit(sha)
    except (GitCommandError, ValueError, KeyError) as exc:
        return {"error": f"No commit '{sha}': {exc}"}

    # A root commit has no parent to diff against, so it is shown against the empty tree.
    parent = commit.parents[0] if commit.parents else None
    diff_text = repository.git.diff(
        parent.hexsha if parent else repository.git.hash_object("-t", "tree", "/dev/null"),
        commit.hexsha,
        unified=3,
    )

    lines = diff_text.splitlines()
    shown, was_truncated = truncated(lines, max_lines)

    return {
        "commit": _describe(commit),
        "diff": "\n".join(shown),
        "truncated": was_truncated,
        "total_lines": len(lines),
    }


@read_only(
    server,
    "Files a commit touched, with how many lines changed in each. Cheaper than the full diff "
    "when the question is only whether a commit went near the failing component.",
)
async def get_changed_files(sha: str, repo: str | None = None) -> dict[str, Any]:
    """Args:
    sha: Commit hash, full or abbreviated.
    repo: Repository path relative to the mount root.
    """
    repository = _repo(repo)

    try:
        commit = repository.commit(sha)
    except (GitCommandError, ValueError, KeyError) as exc:
        return {"error": f"No commit '{sha}': {exc}"}

    return {
        "commit": _describe(commit),
        "files": [
            {
                "path": path,
                "insertions": stats["insertions"],
                "deletions": stats["deletions"],
            }
            for path, stats in commit.stats.files.items()
        ],
    }


@read_only(
    server,
    "Search commit subjects and bodies. Useful for finding when a component was last touched, "
    "or locating the change that introduced a named feature.",
)
async def search_commit_messages(
    query: str,
    repo: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Args:
    query: Text to look for, case-insensitive.
    repo: Repository path relative to the mount root.
    limit: Maximum commits to return.
    """
    repository = _repo(repo)
    capped = min(limit, settings().max_results)

    commits = [
        _describe(c)
        for c in repository.iter_commits(max_count=500)
        if query.lower() in (c.message or "").lower()
    ][:capped]

    if not commits:
        return empty("all history", f"commit messages containing '{query}'", query=query)

    return result(found=len(commits), window="all history", items=commits, query=query)


@read_only(
    server,
    "Every commit that touched one file, newest first. Answers 'when did this file last "
    "change' — the question that follows a stack trace naming a file and a line.",
)
async def get_file_history(path: str, repo: str | None = None, limit: int = 20) -> dict[str, Any]:
    """Args:
    path: Repository-relative file path, e.g. `sample-services/payments/PaymentProcessor.cs`.
    repo: Repository path relative to the mount root.
    limit: Maximum commits to return.
    """
    repository = _repo(repo)
    capped = min(limit, settings().max_results)

    try:
        commits = [_describe(c) for c in repository.iter_commits(paths=path, max_count=capped)]
    except GitCommandError as exc:
        return {"error": f"Could not read history for '{path}': {exc}"}

    if not commits:
        return empty("all history", f"commits touching '{path}'", path=path)

    return result(found=len(commits), window="all history", items=commits, path=path)


@read_only(
    server,
    "Commits between two references. The deployment-correlation tool: given the previous "
    "release and the current one, it lists exactly what went out.",
)
async def get_commits_between(
    from_ref: str,
    to_ref: str = "HEAD",
    repo: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Args:
    from_ref: Older reference — a tag, branch or sha.
    to_ref: Newer reference. Defaults to HEAD.
    repo: Repository path relative to the mount root.
    limit: Maximum commits to return.
    """
    repository = _repo(repo)
    capped = min(limit, settings().max_results)

    try:
        commits = [
            _describe(c)
            for c in repository.iter_commits(f"{from_ref}..{to_ref}", max_count=capped)
        ]
    except (GitCommandError, ValueError) as exc:
        return {"error": f"Could not compare '{from_ref}..{to_ref}': {exc}"}

    if not commits:
        return empty(
            f"{from_ref}..{to_ref}",
            "the range between those references",
            interpretation="The two references point at the same commit, or the range is empty.",
        )

    return result(
        found=len(commits),
        window=f"{from_ref}..{to_ref}",
        items=commits,
    )


def main() -> None:
    serve(server, settings().port)


if __name__ == "__main__":
    main()
