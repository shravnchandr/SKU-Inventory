"""Checks whether a newer version of the app is sitting on the update
source (origin/main) than what's currently checked out — surfaced as a
small banner on every page so the person running this finds out from the
app itself, instead of only ever hearing it secondhand.

Runs the same two git facts update.bat's own "Already up to date" check
uses (git fetch, then compare HEAD to origin/main) — this just does it from
inside the running app so it doesn't take a manual double-click to find
out. Every git call is subprocess-isolated with a timeout and never lets a
failure (git not installed, not a git checkout — e.g. a zip-extracted copy
rather than update.bat's git clone, no configured remote, no network)
propagate: an update check is a nice-to-have, not something that should
ever be able to break a page load.

WHY THIS CAN'T LEAK PHARMACY DATA (verified, not just asserted):

  - The only network call anywhere in this module is `git fetch`. `git
    fetch` is structurally one-directional — it downloads objects the
    remote has that the local repo doesn't; it never uploads local commits,
    files, or refs *to* the remote (that's exclusively what `git push`
    does, which nothing in this module — or anywhere else in this app —
    ever calls). The negotiation phase does tell the remote which commit
    hashes are already known locally, but those are opaque hashes of this
    *source code* repo's own history, not data of any kind.

  - Even granting that, there is nothing to leak: db/, uploads/, output/,
    and logs/ — every place this app's actual pharmacy data ever
    touches disk — are .gitignore'd, and `git log --all --full-history --
    db/ uploads/ output/ logs/` against this repo returns nothing: that
    data has never once been part of this git history, in any commit, on
    any branch. A fetch can only exchange objects that exist in a repo's
    object database; data that was never committed cannot be transmitted
    by any git operation, fetch included.

  - Every other call this module makes (remote get-url, rev-list, log,
    rev-parse) is answered entirely from the local .git directory — no
    network involved at all, regardless of arguments.

  - GIT_TERMINAL_PROMPT=0 below means the fetch can never pop up a
    credential prompt (terminal or OS credential-manager dialog) if auth
    is ever required and not already cached — it just fails cleanly (same
    as any other unreachable-remote case) and the banner doesn't show.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

FETCH_TIMEOUT_SECONDS = 15.0
BRANCH = "main"

# Never let git prompt for credentials — a hung terminal/GUI credential
# prompt is exactly the kind of thing an update check (a nice-to-have that
# must never get in the user's way) has no business risking. A fetch that
# would've needed auth just fails cleanly instead, same as any other
# unreachable-remote case.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


@dataclass(frozen=True)
class UpdateStatus:
    commits_behind: int
    latest_summary: str | None  # one-line subject of the newest commit not yet pulled
    latest_commit: str | None  # short hash — lets the frontend key a "dismissed" flag to it


def _run_git(repo_dir: Path, *args: str, timeout: float) -> str | None:
    """A git subcommand's stdout, or None for any failure at all (missing
    git, not a repo, no remote, no network, timeout, non-zero exit) — every
    caller here treats "couldn't check" and "checked, nothing to report"
    identically, so there's nothing gained by distinguishing them further.
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_GIT_ENV,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def check_for_update(repo_dir: str | Path = ".") -> UpdateStatus | None:
    """None if there's nothing to show — either genuinely up to date, or
    the check itself couldn't be completed. Never raises.
    """
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").exists():
        return None
    if _run_git(repo_dir, "remote", "get-url", "origin", timeout=FETCH_TIMEOUT_SECONDS) is None:
        return None
    if (
        _run_git(repo_dir, "fetch", "origin", BRANCH, "--quiet", timeout=FETCH_TIMEOUT_SECONDS)
        is None
    ):
        return None

    count = _run_git(
        repo_dir, "rev-list", "--count", f"HEAD..origin/{BRANCH}", timeout=FETCH_TIMEOUT_SECONDS
    )
    if count is None or not count.isdigit() or int(count) == 0:
        return None

    summary = _run_git(
        repo_dir, "log", "-1", "--format=%s", f"origin/{BRANCH}", timeout=FETCH_TIMEOUT_SECONDS
    )
    commit = _run_git(
        repo_dir, "rev-parse", "--short", f"origin/{BRANCH}", timeout=FETCH_TIMEOUT_SECONDS
    )
    return UpdateStatus(commits_behind=int(count), latest_summary=summary, latest_commit=commit)
