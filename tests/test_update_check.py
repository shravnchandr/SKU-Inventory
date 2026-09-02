"""check_for_update() against real git repos — not mocked, since the whole
point is verifying the actual git plumbing (fetch, rev-list --count,
log -1) does what this module assumes it does. Also covers every "nothing
to show" path: never allowed to raise, regardless of what's wrong.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from src.gowri_proj.update_check import check_for_update


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    return path


def _commit(path: Path, filename: str, content: str, message: str):
    (path / filename).write_text(content)
    _git("add", ".", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)


def _make_origin_and_clone(tmp_path):
    """An origin repo with one commit, and a local clone of it — both on
    branch `main`, matching what update.bat expects.
    """
    origin = _init_repo(tmp_path / "origin")
    _commit(origin, "f.txt", "1", "first")
    _git("branch", "-M", "main", cwd=origin)

    local = tmp_path / "local"
    _git("clone", "-q", str(origin), str(local), cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=local)
    _git("config", "user.name", "Test", cwd=local)
    return origin, local


def test_up_to_date_reports_no_update(tmp_path):
    _origin, local = _make_origin_and_clone(tmp_path)
    assert check_for_update(local) is None


def test_reports_commits_behind_and_the_latest_summary(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _commit(origin, "f.txt", "2", "second commit")
    _commit(origin, "f.txt", "3", "third commit")

    status = check_for_update(local)
    assert status is not None
    assert status.commits_behind == 2
    assert status.latest_summary == "third commit"
    assert status.latest_commit  # a short hash, non-empty


def test_a_local_commit_origin_does_not_have_does_not_count_as_behind(tmp_path):
    # HEAD..origin/main only counts commits reachable from origin/main but
    # not from HEAD — a purely local, unpushed commit must not show up as
    # "there's an update" (there isn't one; it's the other way around).
    _origin, local = _make_origin_and_clone(tmp_path)
    _commit(local, "g.txt", "local only", "local-only commit")
    assert check_for_update(local) is None


def test_not_a_git_checkout_returns_none(tmp_path):
    # e.g. someone extracted a zip of the source instead of using
    # update.bat's git clone.
    (tmp_path / "some_file.txt").write_text("hi")
    assert check_for_update(tmp_path) is None


def test_no_configured_remote_returns_none(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "f.txt", "1", "first")
    assert check_for_update(repo) is None


def test_git_not_installed_returns_none_not_a_crash(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _commit(origin, "f.txt", "2", "second")
    with patch("subprocess.run", side_effect=FileNotFoundError("git not found")):
        assert check_for_update(local) is None


def test_a_hung_git_call_times_out_instead_of_hanging_the_check(tmp_path):
    origin, local = _make_origin_and_clone(tmp_path)
    _commit(origin, "f.txt", "2", "second")
    with patch(
        "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=15)
    ):
        assert check_for_update(local) is None
