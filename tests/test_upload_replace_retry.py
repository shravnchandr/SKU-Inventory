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
from unittest.mock import patch

import openpyxl
import pytest

from src.gowri_proj.webapp import _replace_with_retry, create_app

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
