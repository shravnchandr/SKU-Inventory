"""Coverage for wiring check_for_update()'s result into the app:
start_update_check() populating app.config in the background, and the
update-available banner (base.html, shared by every page) rendering from
that config value.
"""

import time

import pytest

from src.gowri_proj.update_check import UpdateStatus
from src.gowri_proj.webapp import _update_banner_message, create_app, start_update_check


def test_update_banner_message_pluralizes_correctly():
    singular = _update_banner_message(
        UpdateStatus(commits_behind=1, latest_summary="Fix", latest_commit="abc")
    )
    assert "1 commit behind" in singular
    assert "1 commits" not in singular

    plural = _update_banner_message(
        UpdateStatus(commits_behind=5, latest_summary="Fix", latest_commit="abc")
    )
    assert "5 commits behind" in plural


def test_update_banner_message_omits_summary_when_absent():
    message = _update_banner_message(
        UpdateStatus(commits_behind=2, latest_summary=None, latest_commit="abc")
    )
    assert "latest:" not in message
    assert "2 commits behind" in message


@pytest.fixture
def app(tmp_path):
    return create_app(
        db_path=str(tmp_path / "test.db"),
        uploads_dir=str(tmp_path / "uploads"),
        log_dir=str(tmp_path / "logs"),
    )


def test_no_banner_when_update_status_is_none(app):
    app.config["_UPDATE_STATUS"] = None
    with app.test_client() as c:
        body = c.get("/reports").get_data(as_text=True)
    # base.html's <style> block always defines .update-banner's CSS, so
    # checking for the *element* (its id) is what actually distinguishes
    # "the banner didn't render" from "the banner's styling is present".
    assert 'id="update-banner"' not in body


def test_banner_shows_commits_behind_and_summary(app):
    app.config["_UPDATE_STATUS"] = UpdateStatus(
        commits_behind=3, latest_summary="Fix the thing", latest_commit="abc1234"
    )
    with app.test_client() as c:
        body = c.get("/reports").get_data(as_text=True)
    assert 'id="update-banner"' in body
    assert "3 commits behind" in body
    assert "Fix the thing" in body
    assert 'data-commit="abc1234"' in body


def test_banner_singular_commit_count_reads_naturally(app):
    app.config["_UPDATE_STATUS"] = UpdateStatus(
        commits_behind=1, latest_summary="One fix", latest_commit="def5678"
    )
    with app.test_client() as c:
        body = c.get("/reports").get_data(as_text=True)
    assert "1 commit behind" in body
    assert "1 commits behind" not in body


def test_banner_escapes_a_commit_summary_containing_html(app):
    app.config["_UPDATE_STATUS"] = UpdateStatus(
        commits_behind=1, latest_summary="<script>alert(1)</script>", latest_commit="abc"
    )
    with app.test_client() as c:
        body = c.get("/reports").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_banner_appears_on_every_page_not_just_the_dashboard(app):
    # /dashboard and /trends redirect to /reports until at least one report
    # is imported, so this sticks to pages that render unconditionally —
    # the banner lives in base.html, shared by all of them regardless.
    app.config["_UPDATE_STATUS"] = UpdateStatus(
        commits_behind=1, latest_summary="Fix", latest_commit="abc"
    )
    with app.test_client() as c:
        for path in ("/reports", "/settings"):
            body = c.get(path).get_data(as_text=True)
            assert 'id="update-banner"' in body, path


def test_start_update_check_populates_config_in_the_background(app, tmp_path):
    # No .git here, so check_for_update() itself returns None quickly — this
    # is really testing that start_update_check() actually calls it and
    # writes the result back to app.config without blocking the caller.
    app.config["_UPDATE_STATUS"] = "sentinel-not-yet-overwritten"
    start_update_check(app, repo_dir=str(tmp_path))
    for _ in range(50):
        if app.config["_UPDATE_STATUS"] != "sentinel-not-yet-overwritten":
            break
        time.sleep(0.05)
    assert app.config["_UPDATE_STATUS"] is None
