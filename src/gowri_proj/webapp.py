"""Local web app: everything the CLI can do, driven from a browser instead.

Runs entirely on localhost — no data leaves the machine. Two pages (Dashboard,
Reports) share one nav shell; a handful of small JSON endpoints back the
upload/refresh/remove actions so the pages update without a full reload.
"""

from __future__ import annotations

import logging
import secrets
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request
from markupsafe import escape
from werkzeug.exceptions import HTTPException

from . import db
from .datacache import DataCache
from .routes import register_all

# Re-exported for callers/tests that import them from here (they moved to
# web_helpers.py / routes/pages.py when this module was split up).
from .routes.pages import FAVICON_SVG, STALE_DATA_DAYS
from .sync import (
    DEFAULT_UPLOADS_DIR,
)
from .update_check import UpdateStatus, check_for_update
from .web_helpers import (
    _replace_with_retry,
    _retry_transient_lock,
    _staged_upload,
    item_list_reminder,
    quality_issues_fingerprint,
)

__all__ = [
    "FAVICON_SVG",
    "STALE_DATA_DAYS",
    "_replace_with_retry",
    "_retry_transient_lock",
    "_staged_upload",
    "create_app",
    "item_list_reminder",
    "quality_issues_fingerprint",
    "start_update_check",
]

DEFAULT_DB_PATH = db.DEFAULT_DB_PATH
DEFAULT_LOG_DIR = "logs"
ERROR_LOG_FILENAME = "error.log"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB — generous for a stock statement export


_ERROR_LOG_HANDLER_MARK = "_gowri_error_log_handler"


def _configure_error_log(app: Flask, log_dir: str) -> Path:
    """Write unhandled-error tracebacks to a plain text file, not just
    stderr — the people running this app day to day aren't developers and
    don't have (or know what to do with) a console window; a file they can
    find in the app's own folder and send along is something they actually
    can act on. Bounded size (a few MB across a handful of rotated files)
    so a run of repeated errors can't fill the disk unattended.

    app.logger is looked up by name (logging.getLogger under the hood), so
    it's a process-wide singleton shared by every Flask app with the same
    import name — calling create_app() more than once (every test does)
    would otherwise pile up one more handler on it each time, and every
    subsequent error gets logged that many times over. Drop any handler we
    attached in a previous call before adding this one.
    """
    for old_handler in list(app.logger.handlers):
        if getattr(old_handler, _ERROR_LOG_HANDLER_MARK, False):
            app.logger.removeHandler(old_handler)
            old_handler.close()

    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    log_path = path / ERROR_LOG_FILENAME
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    setattr(handler, _ERROR_LOG_HANDLER_MARK, True)
    app.logger.addHandler(handler)
    return log_path


def _update_banner_message(status: UpdateStatus) -> str:
    """The update-banner's body text, built here rather than assembled
    across multiple lines in the Jinja template — Jinja preserves the
    whitespace/newlines between tags in the source as literal output
    unless every line uses explicit `{%- -%}` trim markers, which turns
    "3 commits behind" into "3 commits\n      behind" the moment the
    template gets re-wrapped for readability. One plain string is also
    simpler to test, and Jinja's autoescaping still covers it as a whole —
    latest_summary (a git commit subject, so arguably attacker-influenced
    if anyone with push access to the fork this points at were untrusted)
    reaches the page exactly like every other {{ value }} on it.
    """
    plural = "" if status.commits_behind == 1 else "s"
    message = f"{status.commits_behind} commit{plural} behind"
    if status.latest_summary:
        message += f' — latest: "{status.latest_summary}"'
    message += ". Double-click update.bat (or update.command on Mac), then restart the app."
    return message


def create_app(
    db_path: str = DEFAULT_DB_PATH,
    uploads_dir: str = DEFAULT_UPLOADS_DIR,
    log_dir: str = DEFAULT_LOG_DIR,
) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["UPLOADS_DIR"] = uploads_dir
    app.config["_SUMMARY_CACHE"] = None
    # (summary, points) for /api/status-history — see that route. Keyed on
    # the summary object's identity: get_current_data() builds a new summary
    # whenever the data or settings change, so "same object" means "still
    # valid" without duplicating its fingerprint logic here.
    app.config["_STATUS_HISTORY_CACHE"] = None
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    app.config["ERROR_LOG_PATH"] = _configure_error_log(app, log_dir)
    # Set for real by start_update_check() (called from app.py's main(), not
    # here — see that function's docstring for why create_app() itself stays
    # network-free) once its background check completes; None until then,
    # and forever if the check never finds anything worth reporting.
    app.config["_UPDATE_STATUS"] = None
    # Regenerated every process start. Not persisted — its only job is to stop
    # a page from some *other* site making mutating requests to this local
    # server (a real risk: this binds to 127.0.0.1 but any tab open in the
    # same browser can still reach it). Simple form submissions from another
    # origin can't set custom headers, so requiring this one blocks that path.
    app.config["CSRF_TOKEN"] = secrets.token_hex(16)
    Path(uploads_dir).mkdir(parents=True, exist_ok=True)
    data = DataCache(app)
    company_name = data.company_name

    @app.context_processor
    def inject_globals():
        update_status = app.config.get("_UPDATE_STATUS")
        return {
            "company": company_name(),
            "csrf_token": app.config["CSRF_TOKEN"],
            "update_status": update_status,
            "update_message": _update_banner_message(update_status) if update_status else None,
        }

    @app.before_request
    def check_csrf():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith(
            "/api/"
        ):
            token = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, app.config["CSRF_TOKEN"]):
                abort(403, description="Missing or invalid CSRF token.")

    @app.errorhandler(403)
    def handle_403(e):
        return jsonify(error=str(e.description) or "Forbidden."), 403

    @app.errorhandler(413)
    def handle_too_large(e):
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify(error=f"That file is larger than the {mb}MB limit."), 413

    @app.errorhandler(Exception)
    def handle_unexpected_error(e):
        # Anything not already handled above (403/413/etc. are HTTPExceptions
        # and pass straight through unchanged) means a genuine bug — a bad
        # file, a locked DB, whatever. The browser used to only ever see a
        # bare "Request failed (500)" with nothing to go on. Surface the
        # actual exception message instead — this app runs single-user on
        # one machine, so there's no other audience to worry about leaking
        # it to — and point at the log *file*, not "the console window":
        # whoever's at the keyboard here runs a pharmacy, not a terminal,
        # and may never have the console open (or know it exists).
        if isinstance(e, HTTPException):
            return e
        app.logger.exception("Unhandled error handling %s %s", request.method, request.path)
        detail = str(e) or e.__class__.__name__
        log_path = app.config.get("ERROR_LOG_PATH", Path(DEFAULT_LOG_DIR) / ERROR_LOG_FILENAME)
        message = (
            f"Something went wrong on this computer while handling that request: {detail}. "
            f"The technical details were saved to the file {log_path} in this app's folder — "
            "if this keeps happening, send that file to whoever set up the app."
        )
        if request.path.startswith("/api/"):
            return jsonify(error=message), 500
        if _error_template_is_safe():
            return render_template("error.html", message=message), 500
        # Plain fallback path (used when the DB is too broken to even render
        # error.html's nav bar) — no Jinja autoescaping here, so the
        # exception text (which can echo attacker-influenced input, e.g. a
        # crafted filename) must be escaped by hand before interpolation.
        return f"<h1>Something went wrong</h1><p>{escape(message)}</p>", 500

    def _error_template_is_safe() -> bool:
        # error.html extends base.html, whose context processor queries the
        # DB for the company name — if *that's* what's broken, rendering the
        # nice error page would itself throw and mask the real error. Fall
        # back to a plain, dependency-free page in that case.
        try:
            company_name()
        except Exception:  # noqa: BLE001 — already inside the unknown-exception
            # handler; can't afford to be picky about which exception types
            # here mean "still broken."
            return False
        return True

    register_all(app, data)
    return app


def start_update_check(app: Flask, repo_dir: str = ".") -> None:
    """Kick off a one-time, one-shot background check of whether origin/main
    has commits this checkout doesn't — the result lands in
    app.config["_UPDATE_STATUS"] whenever it's ready, and every page picks
    it up on its next render via inject_globals()'s context processor.

    Deliberately *not* called from create_app() itself: this is the app's
    only outbound network call (a git fetch against the update source — no
    inventory data, just ref names), and create_app() is called on every
    test run and every accidental re-run — none of which should be doing
    network I/O or spawning background threads as a side effect of just
    building the Flask app object. app.py's main() calls this once, right
    before app.run(), matching "check once, at startup" — not a recurring
    poll while the app is left open, which is all that's actually been
    asked for so far.

    A daemon thread so it can never keep the process alive on its own if
    something hangs (fetch has its own timeout regardless, but this is the
    same belt-and-suspenders as the browser-opening Timer in app.py).
    """

    def _check():
        try:
            app.config["_UPDATE_STATUS"] = check_for_update(repo_dir)
        except Exception:
            # This thread has no caller to report to; letting anything here
            # escape would just be an unraisable-exception warning on
            # stderr for a feature that's explicitly allowed to fail
            # silently.
            app.logger.exception("Update check failed")

    threading.Thread(target=_check, daemon=True).start()
