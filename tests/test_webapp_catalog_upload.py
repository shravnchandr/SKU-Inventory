"""Endpoint-level regression tests for POST /api/upload-item-list's file/DB
consistency in both failure directions:

1. The DB import happens before the saved catalog file is replaced, so a
   failed import can't leave a newer file than what's actually in the DB
   (see webapp.py's api_upload_item_list — "Import to the DB before
   touching the saved file"). Reproduced with a duplicate code within a
   single uploaded file's own rows — a real way to make
   db.import_item_catalog fail (violates item_catalog.code's PRIMARY KEY
   during to_sql) without mocking anything.

2. The opposite direction: if the DB import *succeeds* but the follow-up
   os.replace() fails (disk full, permissions, ...), the DB is rolled back
   to match the still-intact file, rather than the DB and file disagreeing
   the other way. This one needs os.replace mocked — it's staged in the
   same directory as its destination specifically so this practically never
   happens for real, but the code path still needs coverage.
"""
from unittest.mock import patch

import openpyxl
import pytest

from src.gowri_proj import db
from src.gowri_proj.webapp import create_app


def _write_item_list(path, rows, company="JANHAVI MEDICALS", location="BANGALORE", as_of="09/08/2026"):
    """rows: list of either ("brand", name) or ("item", code, product, packing,
    mrp, by_rate, tax_pct, hsn, long_name) tuples.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    sheet2 = wb.create_sheet("Sheet2")
    sheet2.append([company])
    sheet2.append([location])
    sheet2.append([f"Item List as on {as_of}"])
    sheet2.append(["Code", "Product", "Packing", "M.R.P.", "By.Rate", "Tax%", "Sp.Rate", "HSN", "Long Name"])
    for row in rows:
        if row[0] == "brand":
            sheet2.append([row[1]])
        else:
            _, code, product, packing, mrp, by_rate, tax_pct, hsn, long_name = row
            sheet2.append([code, product, packing, mrp, by_rate, tax_pct, None, hsn, long_name])
    wb.save(path)


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    # Deliberately *not* TESTING=True: that flag makes Flask re-raise view
    # exceptions to the test runner instead of turning them into the 500
    # response a real client would get — and that response, not a raised
    # exception, is exactly what this test needs to assert on.
    app.config["PROPAGATE_EXCEPTIONS"] = False
    with app.test_client() as c:
        yield c, app


def _upload(client_and_app, path, filename="item_list.xlsx"):
    client, app = client_and_app
    with open(path, "rb") as f:
        return client.post(
            "/api/upload-item-list",
            data={"file": (f, filename)},
            headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
            content_type="multipart/form-data",
        )


def test_failed_import_leaves_the_existing_catalog_file_and_db_untouched(client, tmp_path):
    good_path = tmp_path / "good.xlsx"
    _write_item_list(
        good_path,
        [
            ("brand", "CIPLA"),
            ("item", "C001", "ARKAMIN 0.1MG TAB", "10S", 20.0, 15.0, 12.0, "3004", "ARKAMIN 0.1MG TABLET"),
        ],
    )
    resp = _upload(client, good_path)
    assert resp.status_code == 200
    assert resp.get_json()["item_count"] == 1

    catalog_path = tmp_path / "uploads" / "item_catalog.xlsx"
    good_bytes = catalog_path.read_bytes()
    with db.connect(str(tmp_path / "test.db")) as conn:
        good_meta = db.get_item_catalog_meta(conn)
    assert good_meta["item_count"] == 1

    # A duplicate code within this one file's own rows — item_catalog.code
    # is a PRIMARY KEY, so the bulk insert inside import_item_catalog raises
    # partway through, well after parse_item_list already succeeded.
    bad_path = tmp_path / "bad.xlsx"
    _write_item_list(
        bad_path,
        [
            ("brand", "CIPLA"),
            ("item", "DUP", "SOMETHING NEW", "10S", 1.0, 1.0, 12.0, "3004", "SOMETHING NEW LONG"),
            ("item", "DUP", "SOMETHING ELSE", "10S", 1.0, 1.0, 12.0, "3004", "SOMETHING ELSE LONG"),
        ],
    )
    resp = _upload(client, bad_path)
    assert resp.status_code == 500

    # The file on disk must still be exactly what the successful upload
    # wrote — not overwritten, not partially written.
    assert catalog_path.read_bytes() == good_bytes
    # And the DB must still reflect the last *successful* import, not a
    # half-applied one (import_item_catalog deletes before it inserts, so a
    # failure partway through the insert would otherwise leave the table
    # empty rather than merely stale).
    with db.connect(str(tmp_path / "test.db")) as conn:
        meta = db.get_item_catalog_meta(conn)
        names = db.list_item_catalog_names(conn)
    assert meta["item_count"] == 1
    assert "ARKAMIN 0.1MG TAB" in names
    assert "SOMETHING NEW" not in names


def test_a_failed_file_replace_rolls_back_the_already_committed_db_import(client, tmp_path):
    good_path = tmp_path / "good.xlsx"
    _write_item_list(
        good_path,
        [
            ("brand", "CIPLA"),
            ("item", "C001", "ARKAMIN 0.1MG TAB", "10S", 20.0, 15.0, 12.0, "3004", "ARKAMIN 0.1MG TABLET"),
        ],
    )
    resp = _upload(client, good_path)
    assert resp.status_code == 200

    catalog_path = tmp_path / "uploads" / "item_catalog.xlsx"
    good_bytes = catalog_path.read_bytes()

    new_path = tmp_path / "new.xlsx"
    _write_item_list(
        new_path,
        [
            ("brand", "GSK"),
            ("item", "G001", "AUGMENTIN 625 TAB", "10S", 200.0, 180.0, 12.0, "3004", "AUGMENTIN 625 DUO TAB"),
        ],
    )
    # The DB import (which happens first) succeeds normally; only the
    # os.replace() that follows it is made to fail, simulating a disk-full/
    # permissions failure on the file write.
    with patch("src.gowri_proj.webapp.os.replace", side_effect=OSError("no space left on device")):
        resp = _upload(client, new_path)
    assert resp.status_code == 500
    assert "catalog was not changed" in resp.get_json()["error"]

    # The file was never touched by the failed replace...
    assert catalog_path.read_bytes() == good_bytes
    # ...and the DB, which *had* already committed the new catalog, must
    # have been rolled back to match it — not left disagreeing the other way.
    with db.connect(str(tmp_path / "test.db")) as conn:
        meta = db.get_item_catalog_meta(conn)
        names = db.list_item_catalog_names(conn)
    assert meta["item_count"] == 1
    assert "ARKAMIN 0.1MG TAB" in names
    assert "AUGMENTIN 625 TAB" not in names
