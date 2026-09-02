"""Regression coverage for _replace_with_retry.

Reproduced live against a real upload: `os.replace()` in api_upload failed
with `PermissionError: [WinError 5] Access is denied` while saving a
freshly-written temp file over an existing report file — Windows Defender's
real-time scan briefly holding a lock on the just-written temp file. Unlike
POSIX rename, Windows refuses to replace a file that's momentarily locked by
another handle, and a single failed attempt used to crash the whole request
as an unhandled 500 with no explanation.
"""

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import openpyxl
import pytest

from src.gowri_proj.webapp import (
    _replace_with_retry,
    _retry_transient_lock,
    _staged_upload,
    create_app,
)

HEADER_ROW = [
    "Item", None, None, None, None,
    "Opening Stock", "Purchase", "Purchase Free", "Other Receipt",
    "Sales", "Sales Free", "Other Issue", None, "Closing Stock", "Value",
]  # fmt: skip


def _write_stock_statement(path, company="TEST PHARMACY", location="TEST CITY"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([company])
    ws.append([location])
    ws.append(["Stock Statement from 01/Aug/2026 to 31/Aug/2026"])
    ws.append(HEADER_ROW)
    ws.append(["BRAND A"])
    row = [None] * 15
    row[0], row[5], row[6], row[9], row[13], row[14] = "SKU ONE", 10, 5, 3, 12, 120.0
    ws.append(row)
    ws.append(["Sub Total"])
    wb.save(path)


def test_replace_retries_past_a_transient_permission_error(tmp_path):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new")
    dest.write_text("old")

    real_replace = __import__("os").replace
    calls = {"n": 0}

    def flaky_replace(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return real_replace(a, b)

    with patch("src.gowri_proj.webapp.os.replace", side_effect=flaky_replace):
        _replace_with_retry(src, dest, attempts=5, delay=0)

    assert calls["n"] == 3
    assert dest.read_text() == "new"


def test_replace_gives_up_and_raises_a_plain_message_after_repeated_failures(tmp_path):
    src = tmp_path / "src.txt"
    dest = tmp_path / "dest.txt"
    src.write_text("new")
    dest.write_text("old")

    with patch(
        "src.gowri_proj.webapp.os.replace",
        side_effect=PermissionError(5, "Access is denied"),
    ), pytest.raises(OSError, match="Windows wouldn't let this file be saved"):
        _replace_with_retry(src, dest, attempts=3, delay=0)

    # The original file must still be intact — a failed replace shouldn't
    # have touched it.
    assert dest.read_text() == "old"


def test_retry_transient_lock_retries_past_a_permission_error(tmp_path):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        return "done"

    assert _retry_transient_lock(flaky, attempts=5, delay=0) == "done"
    assert calls["n"] == 3


def test_retry_transient_lock_rejects_attempts_below_one():
    # attempts=0 would skip the retry loop entirely and hit `raise
    # last_error` with last_error still None — a bare, confusing TypeError
    # instead of a clear failure. No call site passes this today (all use
    # the default of 5), but the function should refuse it outright rather
    # than depend on that.
    with pytest.raises(ValueError, match="attempts must be at least 1"):
        _retry_transient_lock(lambda: "unreachable", attempts=0)


def test_retry_transient_lock_gives_up_and_reraises_the_original_error(tmp_path):
    def always_locked():
        raise PermissionError(5, "Access is denied")

    with pytest.raises(PermissionError, match="Access is denied"):
        _retry_transient_lock(always_locked, attempts=3, delay=0)


def test_upload_endpoint_retries_past_a_flaky_stat_after_a_successful_replace(tmp_path):
    # The replace itself succeeds; only the immediate stat() read-back
    # (used for the watched_files fingerprint) is momentarily locked. This
    # must not fail the upload — a few retries and it should import
    # normally, exactly like a flaky os.replace does.
    app = create_app(
        db_path=str(tmp_path / "test.db"),
        uploads_dir=str(tmp_path / "uploads"),
        log_dir=str(tmp_path / "logs"),
    )
    app.config["PROPAGATE_EXCEPTIONS"] = False
    path = tmp_path / "stocknsales0826.xls"
    _write_stock_statement(path)

    real_stat = Path.stat
    calls = {"n": 0}

    def flaky_stat(self, *args, **kwargs):
        if self.suffix == ".xls" and not self.name.startswith("."):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
        return real_stat(self, *args, **kwargs)

    with app.test_client() as c, patch.object(Path, "stat", flaky_stat), open(path, "rb") as f:
        resp = c.post(
            "/api/upload",
            data={"file": (io.BytesIO(f.read()), "stocknsales0826.xls")},
            headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
            content_type="multipart/form-data",
        )

    assert resp.status_code == 200
    assert calls["n"] == 3


def test_upload_endpoint_reports_a_clean_message_when_stat_stays_locked(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "test.db"),
        uploads_dir=str(tmp_path / "uploads"),
        log_dir=str(tmp_path / "logs"),
    )
    app.config["PROPAGATE_EXCEPTIONS"] = False
    path = tmp_path / "stocknsales0826.xls"
    _write_stock_statement(path)

    real_stat = Path.stat

    def always_locked(self, *args, **kwargs):
        if self.suffix == ".xls" and not self.name.startswith("."):
            raise PermissionError(5, "Access is denied")
        return real_stat(self, *args, **kwargs)

    with app.test_client() as c, patch.object(Path, "stat", always_locked), open(path, "rb") as f:
        resp = c.post(
            "/api/upload",
            data={"file": (io.BytesIO(f.read()), "stocknsales0826.xls")},
            headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
            content_type="multipart/form-data",
        )

    assert resp.status_code == 500
    body = resp.get_json()
    assert "couldn't read it back" in body["error"].lower()


def test_upload_endpoint_surfaces_a_clean_message_instead_of_a_bare_500(tmp_path):
    app = create_app(
        db_path=str(tmp_path / "test.db"),
        uploads_dir=str(tmp_path / "uploads"),
        log_dir=str(tmp_path / "logs"),
    )
    app.config["PROPAGATE_EXCEPTIONS"] = False
    path = tmp_path / "stocknsales0826.xls"
    _write_stock_statement(path)

    with app.test_client() as c, patch(
        "src.gowri_proj.webapp.os.replace",
        side_effect=PermissionError(5, "Access is denied"),
    ), open(path, "rb") as f:
        resp = c.post(
            "/api/upload",
            data={"file": (io.BytesIO(f.read()), "stocknsales0826.xls")},
            headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
            content_type="multipart/form-data",
        )

    assert resp.status_code == 500
    body = resp.get_json()
    assert "antivirus" in body["error"].lower() or "locked" in body["error"].lower()


def test_staged_upload_cleanup_failure_does_not_override_the_caller_response(tmp_path):
    """_staged_upload always unlinks the temp file in `finally` — if the
    same lock that made a caller's _replace_with_retry give up also blocks
    *that* unlink, an exception raised there would silently override
    whatever specific response the caller was already returning from
    inside the `with` block (confirmed by hand: raising from a
    @contextmanager's finally during __exit__ replaces a return already
    pending in the calling scope). The fix must swallow a failed cleanup so
    the caller's own return value survives.
    """
    file = MagicMock()
    file.save = lambda f: f.write(b"data")

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name.startswith(".upload-"):
            raise PermissionError(5, "Access is denied")
        return real_unlink(self, *args, **kwargs)

    def caller():
        with _staged_upload(file, "report.xls", tmp_path):
            return "the caller's specific error message"

    with patch.object(Path, "unlink", flaky_unlink):
        assert caller() == "the caller's specific error message"
