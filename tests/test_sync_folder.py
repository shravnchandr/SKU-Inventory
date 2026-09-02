"""sync_folder() regression coverage.

Audit finding: path.stat() ran outside any per-file error handling, so a
single file that's locked (antivirus, Excel) or vanishes between the
rglob() listing and here (moved/deleted mid-scan) would raise straight out
of sync_folder — aborting the *entire* refresh instead of reporting just
that one file and importing everything else, exactly the failure mode the
per-file try/except blocks lower down already exist to avoid for parse
errors.
"""

from pathlib import Path
from unittest.mock import patch

import openpyxl

from src.gowri_proj import db
from src.gowri_proj.sync import sync_folder

HEADER_ROW = [
    "Item", None, None, None, None,
    "Opening Stock", "Purchase", "Purchase Free", "Other Receipt",
    "Sales", "Sales Free", "Other Issue", None, "Closing Stock", "Value",
]  # fmt: skip


def _write_stock_statement(path, sku="SKU ONE", company="TEST PHARMACY", location="TEST CITY"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([company])
    ws.append([location])
    ws.append(["Stock Statement from 01/Aug/2026 to 31/Aug/2026"])
    ws.append(HEADER_ROW)
    ws.append(["BRAND A"])
    row = [None] * 15
    row[0], row[5], row[6], row[9], row[13], row[14] = sku, 10, 5, 3, 12, 120.0
    ws.append(row)
    ws.append(["Sub Total"])
    wb.save(path)


def test_a_locked_or_vanished_file_is_reported_without_aborting_the_rest_of_the_scan(tmp_path):
    good_path = tmp_path / "good.xlsx"
    locked_path = tmp_path / "locked.xlsx"
    _write_stock_statement(good_path, sku="GOOD SKU")
    _write_stock_statement(locked_path, sku="LOCKED SKU")

    # is_file() (during the candidate scan) needs its own stat() call to
    # succeed, or the file wouldn't even be picked up as a candidate — only
    # the *second* stat call against it (the explicit one in sync_folder's
    # loop, reading the fingerprint) should look locked, matching what a
    # real transient lock looks like: the file is there and listable, just
    # not readable at that exact moment.
    real_stat = Path.stat
    call_counts: dict[str, int] = {}

    def flaky_stat(self, *args, **kwargs):
        if self.name == "locked.xlsx":
            call_counts[self.name] = call_counts.get(self.name, 0) + 1
            if call_counts[self.name] > 1:
                raise PermissionError(5, "Access is denied")
        return real_stat(self, *args, **kwargs)

    with db.connect(str(tmp_path / "test.db")) as conn, patch.object(Path, "stat", flaky_stat):
        result = sync_folder(conn, str(tmp_path))

    assert result.imported == [("good.xlsx", "2026-08-01", "2026-08-31", 1)]
    assert len(result.errors) == 1
    assert result.errors[0][0] == "locked.xlsx"
    assert "Access is denied" in result.errors[0][1]

    with db.connect(str(tmp_path / "test.db")) as conn:
        reports = db.list_reports(conn)
    assert len(reports) == 1
    assert reports.iloc[0]["source_filename"] == "good.xlsx"


def test_a_previously_locked_file_is_retried_and_imported_on_a_later_scan(tmp_path):
    # Nothing should have been recorded against it in watched_files that
    # would stop a later, unlocked scan from picking it up normally.
    path = tmp_path / "was_locked.xlsx"
    _write_stock_statement(path)

    real_stat = Path.stat
    call_counts: dict[str, int] = {}

    def flaky_stat(self, *args, **kwargs):
        if self.name == "was_locked.xlsx":
            call_counts[self.name] = call_counts.get(self.name, 0) + 1
            if call_counts[self.name] > 1:
                raise PermissionError(5, "Access is denied")
        return real_stat(self, *args, **kwargs)

    with db.connect(str(tmp_path / "test.db")) as conn:
        with patch.object(Path, "stat", flaky_stat):
            sync_folder(conn, str(tmp_path))
        # Now unlocked — a normal rescan should import it cleanly.
        result = sync_folder(conn, str(tmp_path))

    assert result.imported == [("was_locked.xlsx", "2026-08-01", "2026-08-31", 1)]
