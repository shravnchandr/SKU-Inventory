"""sync_folder() with two files in uploads/ whose periods overlap.

Found on real data: uploads/2627/ held both stocknsales0826.xls (Aug 1-17,
saved Aug 17) and stocknsales_9.8.xls (Aug 1-9, saved Aug 9). The scan
walks files alphabetically and import_report lets the newest import win, so
the 9-day file — sorted last, but saved *earlier* — always ended up as "the"
August report. Worse, each import deleted the other's report and with it the
other file's watched_files fingerprint, so every rescan re-imported both
files and flipped August again.

"Newest wins" now means most recently *saved* (file mtime), and the losing
file is fingerprinted as superseded, so later scans leave it alone.
"""

import os
from datetime import datetime

import openpyxl
import pandas as pd

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.sync import sync_folder

HEADER_ROW = [
    "Item", None, None, None, None,
    "Opening Stock", "Purchase", "Purchase Free", "Other Receipt",
    "Sales", "Sales Free", "Other Issue", None, "Closing Stock", "Value",
]  # fmt: skip


def _write(path, start, end, saved_at, sku="SKU ONE"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["TEST PHARMACY"])
    ws.append(["TEST CITY"])
    ws.append([f"Stock Statement from {start} to {end}"])
    ws.append(HEADER_ROW)
    ws.append(["BRAND A"])
    row = [None] * 15
    row[0], row[5], row[6], row[9], row[13], row[14] = sku, 10, 5, 3, 12, 120.0
    ws.append(row)
    wb.save(path)
    ts = datetime.fromisoformat(saved_at).timestamp()
    os.utime(path, (ts, ts))


def _reports(conn):
    r = db.list_reports(conn)
    return [
        (row["period_start"].date().isoformat(), row["period_end"].date().isoformat(),
         row["source_filename"])
        for _, row in r.iterrows()
    ]  # fmt: skip


def _real_world_pair(folder):
    # Same shape as the real files: the longer, newer file sorts *first*.
    _write(folder / "stocknsales0826.xlsx", "01/Aug/2026", "17/Aug/2026", "2026-08-17T08:58")
    _write(folder / "stocknsales_9.8.xlsx", "01/Aug/2026", "09/Aug/2026", "2026-08-09T08:52")


def test_the_more_recently_saved_file_wins_regardless_of_name_order(tmp_path):
    _real_world_pair(tmp_path)
    with db.connect(str(tmp_path / "test.db")) as conn:
        result = sync_folder(conn, str(tmp_path))
        assert _reports(conn) == [("2026-08-01", "2026-08-17", "stocknsales0826.xlsx")]
    assert [r[0] for r in result.imported] == ["stocknsales0826.xlsx"]
    assert result.superseded == [
        ("stocknsales_9.8.xlsx", "2026-08-01 to 2026-08-09", "stocknsales0826.xlsx")
    ]


def test_rescanning_is_stable_rather_than_flipping_between_the_two(tmp_path):
    _real_world_pair(tmp_path)
    with db.connect(str(tmp_path / "test.db")) as conn:
        sync_folder(conn, str(tmp_path))
        for _ in range(3):
            again = sync_folder(conn, str(tmp_path))
            assert again.imported == [] and again.superseded == []
            assert sorted(again.unchanged) == ["stocknsales0826.xlsx", "stocknsales_9.8.xlsx"]
        assert _reports(conn) == [("2026-08-01", "2026-08-17", "stocknsales0826.xlsx")]


def test_an_already_wrong_database_is_repaired_by_the_next_rescan(tmp_path):
    # The state real databases are in today: the older partial file won.
    _real_world_pair(tmp_path)
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(
            conn,
            pd.DataFrame([{c: 0.0 for c in TIDY_COLUMNS} | {"brand": "B", "sku": "S"}]),
            ReportMeta("TEST PHARMACY", "TEST CITY", pd.Timestamp("2026-08-01").date(),
                       pd.Timestamp("2026-08-09").date()),
            "stocknsales_9.8.xlsx",
        )  # fmt: skip
        st = (tmp_path / "stocknsales_9.8.xlsx").stat()
        db.upsert_watched_file(
            conn, "stocknsales_9.8.xlsx", st.st_size, int(st.st_mtime),
            db.list_reports(conn).iloc[0]["id"].item(), "imported",
        )  # fmt: skip
        sync_folder(conn, str(tmp_path))
        assert _reports(conn) == [("2026-08-01", "2026-08-17", "stocknsales0826.xlsx")]


def test_a_newer_partial_file_still_wins_newest_upload_rule_unchanged(tmp_path):
    # If the shorter file really is the more recent save, it's the one
    # trusted — the existing "newest upload wins" rule, just measured by
    # save time now.
    _write(tmp_path / "a_full.xlsx", "01/Aug/2026", "31/Aug/2026", "2026-09-01T09:00")
    _write(tmp_path / "b_partial.xlsx", "01/Aug/2026", "09/Aug/2026", "2026-09-02T09:00")
    with db.connect(str(tmp_path / "test.db")) as conn:
        sync_folder(conn, str(tmp_path))
        assert _reports(conn) == [("2026-08-01", "2026-08-09", "b_partial.xlsx")]
        again = sync_folder(conn, str(tmp_path))
        assert again.imported == []
        assert _reports(conn) == [("2026-08-01", "2026-08-09", "b_partial.xlsx")]


def test_removing_the_winning_report_lets_the_superseded_file_back_in(tmp_path):
    _real_world_pair(tmp_path)
    with db.connect(str(tmp_path / "test.db")) as conn:
        sync_folder(conn, str(tmp_path))
        winner_id = db.list_reports(conn).iloc[0]["id"].item()
        db.delete_report(conn, winner_id)
        (tmp_path / "stocknsales0826.xlsx").unlink()
        sync_folder(conn, str(tmp_path))
        assert _reports(conn) == [("2026-08-01", "2026-08-09", "stocknsales_9.8.xlsx")]


def test_superseded_file_is_listed_with_a_reason_in_import_health(tmp_path):
    _real_world_pair(tmp_path)
    with db.connect(str(tmp_path / "test.db")) as conn:
        sync_folder(conn, str(tmp_path))
        problems = db.list_watched_files_problems(conn)
    row = problems[problems["filename"] == "stocknsales_9.8.xlsx"].iloc[0]
    assert row["status"] == "superseded"
    assert "stocknsales0826.xlsx" in row["detail"]
    assert "more recently saved" in row["detail"]


def test_a_report_with_no_file_fingerprint_is_still_replaced_as_before(tmp_path):
    # e.g. imported via the CLI from outside uploads/: no saved-at time to
    # compare, so a scanned overlapping file replaces it — unchanged behavior.
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(
            conn,
            pd.DataFrame([{c: 0.0 for c in TIDY_COLUMNS} | {"brand": "B", "sku": "S"}]),
            ReportMeta("TEST PHARMACY", "TEST CITY", pd.Timestamp("2026-08-01").date(),
                       pd.Timestamp("2026-08-31").date()),
            "elsewhere.xls",
        )  # fmt: skip
        _write(tmp_path / "partial.xlsx", "01/Aug/2026", "09/Aug/2026", "2020-01-01T00:00")
        sync_folder(conn, str(tmp_path))
        assert _reports(conn) == [("2026-08-01", "2026-08-09", "partial.xlsx")]
