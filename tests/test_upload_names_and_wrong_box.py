"""Uploads: a file named in a non-Latin script is accepted (it used to be
rejected as "not .xls/.xlsx"), and a file put in the wrong upload box gets
a message saying so instead of a cryptic parse error."""

import pytest

from src.gowri_proj.webapp import create_app
from tests.test_sync_folder import _write_stock_statement
from tests.test_webapp_catalog_upload import _write_item_list

ITEMS = [("brand", "BRAND A"), ("item", "X1", "X TAB", "10", 1.0, 1.0, 12.0, "3004", None)]


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), uploads_dir=str(tmp_path / "uploads"))
    return app.test_client(), {"X-CSRF-Token": app.config["CSRF_TOKEN"]}, tmp_path / "uploads"


def _post(c, h, endpoint, path, name):
    with open(path, "rb") as f:
        return c.post(
            endpoint, data={"file": (f, name)}, content_type="multipart/form-data", headers=h
        )


def test_non_latin_filename_is_accepted_and_tracked_stably(client, tmp_path):
    c, h, uploads = client
    _write_stock_statement(tmp_path / "s.xlsx")
    r = _post(c, h, "/api/upload", tmp_path / "s.xlsx", "स्टॉक अगस्त.xlsx")
    assert r.status_code == 200, r.get_json()
    saved = [p.name for p in uploads.rglob("upload_*.xlsx")]
    assert len(saved) == 1
    # The same file again (a corrected re-export) replaces it, no duplicate.
    r2 = _post(c, h, "/api/upload", tmp_path / "s.xlsx", "स्टॉक अगस्त.xlsx")
    assert r2.status_code == 200, r2.get_json()
    assert [p.name for p in uploads.rglob("upload_*.xlsx")] == saved


def test_other_extensions_still_rejected(client, tmp_path):
    c, h, _ = client
    (tmp_path / "x.csv").write_text("a,b")
    assert _post(c, h, "/api/upload", tmp_path / "x.csv", "स्टॉक.csv").status_code == 400


def test_item_list_in_the_statement_box(client, tmp_path):
    c, h, _ = client
    _write_item_list(tmp_path / "il.xlsx", ITEMS)
    r = _post(c, h, "/api/upload", tmp_path / "il.xlsx", "il.xlsx")
    assert r.status_code == 422
    assert r.get_json()["error"].startswith("This is the item list, not a stock statement")


def test_statement_in_the_item_list_box(client, tmp_path):
    c, h, _ = client
    _write_stock_statement(tmp_path / "s.xlsx")
    r = _post(c, h, "/api/upload-item-list", tmp_path / "s.xlsx", "s.xlsx")
    assert r.status_code == 422
    assert r.get_json()["error"].startswith("This is a stock statement, not the item list")


def test_genuinely_broken_file_keeps_the_plain_read_error(client, tmp_path):
    c, h, _ = client
    (tmp_path / "b.xlsx").write_bytes(b"nope")
    r = _post(c, h, "/api/upload", tmp_path / "b.xlsx", "b.xlsx")
    assert (
        r.status_code == 422
        and "Couldn't open this file as an Excel spreadsheet" in r.get_json()["error"]
    )
