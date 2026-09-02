"""Regression tests for webapp.py's generic error handling:

1. An unhandled exception on an /api/ route returns a JSON `error` message a
   non-technical user can act on (not a bare "Request failed (500)"), and
   the same exception text is HTML-escaped when it has to fall back to a
   plain (non-Jinja) error page.
2. create_app() is called once per process in production but many times in
   this test suite (and once per accidental re-run in dev) — app.logger is
   looked up by name, so it's a shared, process-wide logger. Without a
   guard, each create_app() call would pile another RotatingFileHandler
   onto it, and every subsequent error would get written to the log file
   that many times over.
"""

from unittest.mock import patch

import pytest

from src.gowri_proj.webapp import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "test.db"),
        uploads_dir=str(tmp_path / "uploads"),
        log_dir=str(tmp_path / "logs"),
    )
    # Deliberately *not* TESTING=True — see test_webapp_catalog_upload.py's
    # fixture for why: this suite needs the real error *response*, not a
    # raised exception propagated to the test runner.
    with app.test_client() as c:
        yield c, tmp_path


def test_unexpected_api_error_returns_a_helpful_json_message(client):
    c, _ = client
    with patch("src.gowri_proj.webapp.db.list_reports", side_effect=RuntimeError("disk on fire")):
        resp = c.get("/api/reports")
    assert resp.status_code == 500
    body = resp.get_json()
    assert "disk on fire" in body["error"]
    assert "console" not in body["error"].lower()  # no dead end for a non-technical user
    assert "logs" in body["error"] and "error.log" in body["error"]


def test_unexpected_page_error_falls_back_to_escaped_plain_html(client):
    c, _ = client
    # Patching db.connect itself (rather than a specific query function)
    # breaks both the dashboard's own data load *and* company_name()'s
    # fallback lookup the same way — so _error_template_is_safe() also
    # fails, forcing the plain, non-Jinja-autoescaped fallback path that
    # this test actually needs to exercise.
    with patch(
        "src.gowri_proj.webapp.db.connect",
        side_effect=RuntimeError("<script>alert(1)</script>"),
    ):
        resp = c.get("/dashboard")
    assert resp.status_code == 500
    body = resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body


def test_repeated_create_app_does_not_duplicate_log_handlers(tmp_path):
    log_dir = tmp_path / "logs"
    app1 = create_app(
        db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"), log_dir=str(log_dir)
    )
    app2 = create_app(
        db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"), log_dir=str(log_dir)
    )
    # Both apps share the same underlying named logger (Flask's app.logger
    # is logging.getLogger(import_name)) — the second create_app() call must
    # have removed the first's handler, not added a second one alongside it.
    assert app1.logger is app2.logger
    error_handlers = [
        h for h in app1.logger.handlers if getattr(h, "_gowri_error_log_handler", False)
    ]
    assert len(error_handlers) == 1

    with (
        app2.test_client() as c,
        patch("src.gowri_proj.webapp.db.list_reports", side_effect=RuntimeError("boom")),
    ):
        c.get("/api/reports")

    log_text = (log_dir / "error.log").read_text()
    assert log_text.count("RuntimeError: boom") == 1
