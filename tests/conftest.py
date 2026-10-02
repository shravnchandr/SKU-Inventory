"""Shared test setup."""

import pytest

from src.gowri_proj import webapp


@pytest.fixture(autouse=True)
def _error_log_in_tmp(tmp_path, monkeypatch):
    """Keep tests out of the real logs/error.log.

    create_app() writes unhandled errors to logs/error.log in the working
    directory unless given a log_dir. Most tests don't pass one, so every run
    appended its deliberately-triggered failures (e.g. a duplicate item code)
    to the app's own error log — the file the pharmacy is told to send when
    something goes wrong — burying any real error in test noise. Tests that
    do pass a log_dir (to check the log itself) are left alone.
    """
    real = webapp._configure_error_log

    def redirected(app, log_dir):
        if log_dir == webapp.DEFAULT_LOG_DIR:
            log_dir = str(tmp_path / "logs")
        return real(app, log_dir)

    monkeypatch.setattr(webapp, "_configure_error_log", redirected)
