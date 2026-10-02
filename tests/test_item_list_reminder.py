"""Reminder to upload a fresh item list when it's older than the latest
stock statement. Rename detection relies on the item list's codes and only
sees a rename once a list from after it has been uploaded, so the list is
meant to be uploaded every month alongside the statement."""

from datetime import date
from unittest.mock import patch

import openpyxl
import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.webapp import create_app, item_list_reminder
from tests.test_webapp_catalog_upload import _write_item_list

MESSAGE_9_17 = (
    "Your item list is from 9 Aug and the latest report is 17 Aug. "
    "Upload a fresh one so renames are recognised automatically."
)


def _meta(as_of, from_file=True):
    return {
        "imported_at": "2026-08-09 10:00:00",
        "item_count": 1,
        "as_of": as_of,
        "as_of_from_file": from_file,
    }


def test_stale_list_message_matches_the_wording():
    r = item_list_reminder(_meta("2026-08-09"), date(2026, 8, 17))
    assert r["kind"] == "stale" and r["message"] == MESSAGE_9_17


@pytest.mark.parametrize("as_of", ["2026-08-17", "2026-08-20"])
def test_list_as_new_as_the_latest_report_needs_no_reminder(as_of):
    assert item_list_reminder(_meta(as_of), date(2026, 8, 17)) is None


def test_year_shown_when_the_dates_are_in_different_years():
    r = item_list_reminder(_meta("2025-12-20"), date(2026, 1, 31))
    assert "from 20 Dec 2025 and the latest report is 31 Jan 2026" in r["message"]


def test_no_list_at_all():
    r = item_list_reminder(None, date(2026, 8, 17))
    assert r["kind"] == "missing" and "No item list uploaded yet" in r["message"]


def test_no_reports_no_reminder():
    assert item_list_reminder(None, None) is None


# ---------- end to end ----------


def _write_statement(path, start="01/Aug/2026", end="17/Aug/2026"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["JANHAVI MEDICALS"])
    ws.append(["BANGALORE"])
    ws.append([f"Stock Statement from {start} to {end}"])
    ws.append(["Item", None, None, None, None, "Opening Stock", "Purchase", "Purchase Free",
               "Other Receipt", "Sales", "Sales Free", "Other Issue", None, "Closing Stock", "Value"])  # fmt: skip
    ws.append(["BRAND A"])
    row = [None] * 15
    row[0], row[5], row[9], row[13], row[14] = "X TAB", 10, 2, 8, 80.0
    ws.append(row)
    wb.save(path)


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), uploads_dir=str(tmp_path / "uploads"))
    app.config["PROPAGATE_EXCEPTIONS"] = False
    with app.test_client() as c:
        yield c, app


def _post_file(c, app, endpoint, path, name):
    with open(path, "rb") as f:
        return c.post(endpoint, data={"file": (f, name)}, content_type="multipart/form-data",
                      headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]})  # fmt: skip


ITEMS = [("brand", "BRAND A"), ("item", "X1", "X TAB", "10", 1.0, 1.0, 12.0, "3004", None)]


def test_upload_flow(client, tmp_path):
    c, app = client
    # A stock statement with no item list at all yet: "missing".
    _write_statement(tmp_path / "aug.xlsx")
    resp = _post_file(c, app, "/api/upload", tmp_path / "aug.xlsx", "aug.xlsx").get_json()
    assert resp["item_list_reminder"]["kind"] == "missing"

    # Item list dated 9 Aug, latest report ends 17 Aug: stale, everywhere.
    _write_item_list(tmp_path / "il.xlsx", ITEMS, as_of="09/08/2026")
    assert (
        _post_file(c, app, "/api/upload-item-list", tmp_path / "il.xlsx", "il.xlsx").status_code
        == 200
    )
    health = c.get("/api/import-health").get_json()
    assert (
        health["item_catalog"]["as_of"] == "2026-08-09"
        and health["item_catalog"]["as_of_from_file"]
    )
    assert health["item_list_reminder"]["message"] == MESSAGE_9_17

    # A fresh list dated 17 Aug clears it.
    _write_item_list(tmp_path / "il2.xlsx", ITEMS, as_of="17/08/2026")
    _post_file(c, app, "/api/upload-item-list", tmp_path / "il2.xlsx", "il2.xlsx")
    assert c.get("/api/import-health").get_json()["item_list_reminder"] is None


def test_failed_item_list_save_does_not_record_its_date(client, tmp_path):
    c, app = client
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(
            conn,
            pd.DataFrame([{c_: 0.0 for c_ in TIDY_COLUMNS} | {"brand": "B", "sku": "X TAB", "closing_stock": 1.0}]),
            ReportMeta("T", "T", date(2026, 8, 1), date(2026, 8, 17)), "aug.xls",
        )  # fmt: skip
    _write_item_list(tmp_path / "il.xlsx", ITEMS, as_of="09/08/2026")
    _post_file(c, app, "/api/upload-item-list", tmp_path / "il.xlsx", "il.xlsx")
    _write_item_list(tmp_path / "il2.xlsx", ITEMS, as_of="20/08/2026")
    with patch("src.gowri_proj.webapp.os.replace", side_effect=PermissionError("locked")):
        assert (
            _post_file(
                c, app, "/api/upload-item-list", tmp_path / "il2.xlsx", "il2.xlsx"
            ).status_code
            == 500
        )
    # Still the 9 Aug list — the rolled-back upload's 20 Aug date didn't stick.
    assert c.get("/api/import-health").get_json()["item_catalog"]["as_of"] == "2026-08-09"


def test_list_without_a_date_falls_back_to_upload_day(client, tmp_path):
    c, app = client
    with db.connect(app.config["DB_PATH"]) as conn:
        db.set_item_catalog_as_of(conn, date(2026, 8, 9))  # an older upload's date…
    _write_item_list(tmp_path / "il.xlsx", ITEMS, as_of="")  # …then a list with no date
    _post_file(c, app, "/api/upload-item-list", tmp_path / "il.xlsx", "il.xlsx")
    with db.connect(app.config["DB_PATH"]) as conn:
        meta = db.get_item_catalog_meta(conn)
    assert meta["as_of_from_file"] is False  # the old 9 Aug didn't linger
