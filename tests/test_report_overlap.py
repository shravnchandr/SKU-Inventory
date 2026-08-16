"""db.import_report's handling of overlapping report periods.

Exact-period duplicates were already blocked (unless replace=True). This
covers the two new cases for a *partial* overlap between a new upload and
an already-imported report:

- superseded: the new upload's range reaches at least as far as an existing
  overlapping report's — e.g. a progressively re-exported "first week" ->
  "first two weeks" -> full month, which the user described as their normal
  workflow. Auto-resolved: the older, now-redundant report is deleted and
  replaced.
- blocking: a partial overlap where the new upload does *not* reach as far
  as an existing report already does — there's no automatic way to decide
  which side is right for the disputed dates, so the whole import is
  rejected with a clear message instead of silently corrupting sales-pace/
  days-of-cover math (summing each report's full period_days and full sales
  assumes reports never overlap).
"""

from datetime import date

import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta


def _meta(start, end):
    return ReportMeta(
        company="TEST PHARMACY",
        location="TEST CITY",
        period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end),
    )


def _df(*skus):
    rows = [
        {
            "brand": "BRAND",
            "sku": sku,
            "opening_stock": 1.0,
            "purchase": 0.0,
            "purchase_free": 0.0,
            "other_receipt": 0.0,
            "sales": 1.0,
            "sales_free": 0.0,
            "other_issue": 0.0,
            "closing_stock": 1.0,
            "value": 10.0,
        }
        for sku in skus
    ]
    return pd.DataFrame(rows, columns=TIDY_COLUMNS)


def test_a_wider_upload_supersedes_a_narrower_overlapping_report(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as conn:
        week1 = db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-07"), "week1.xlsx")
        result = db.import_report(
            conn, _df("A", "B"), _meta("2026-06-01", "2026-06-14"), "week1-2.xlsx"
        )

        reports = db.list_reports(conn)

    assert result.superseded_report_ids == [week1.report_id]
    assert result.replaced is True
    # The old report is actually gone, not just shadowed.
    assert list(reports["id"]) == [result.report_id]
    assert result.sku_count == 2


def test_superseding_clears_the_old_reports_watched_file_fingerprint(tmp_path):
    # A different source filename than the superseded report's own — its
    # watched_files row (if any) has to be cleaned up too, or it would point
    # at a report_id that no longer exists.
    with db.connect(str(tmp_path / "test.db")) as conn:
        week1 = db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-07"), "week1.xlsx")
        db.upsert_watched_file(conn, "week1.xlsx", 100, 1000, week1.report_id, "imported")

        db.import_report(conn, _df("A", "B"), _meta("2026-06-01", "2026-06-14"), "week1-2.xlsx")

        watched = db.get_watched_file(conn, "week1.xlsx")
    assert watched is None


def test_a_narrower_upload_that_does_not_reach_an_existing_reports_end_is_blocked(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A", "B"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")

        with pytest.raises(ValueError, match="partially overlaps"):
            db.import_report(conn, _df("A"), _meta("2026-06-10", "2026-06-20"), "mid_june.xlsx")

        reports = db.list_reports(conn)
    # Nothing changed — the conflicting upload never landed.
    assert len(reports) == 1
    assert reports.iloc[0]["source_filename"] == "june.xlsx"


def test_an_upload_that_starts_later_than_an_existing_report_does_not_supersede_it(tmp_path):
    # Existing 2026-06-01..06-30; new upload 2026-06-15..07-15 ends later
    # but *starts* after the old report already did — reaching only as far
    # as the old report's end isn't supersession, since days 1-14 would be
    # silently lost if the old report were deleted. Must block instead.
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")

        with pytest.raises(ValueError, match="partially overlaps"):
            db.import_report(
                conn, _df("A"), _meta("2026-06-15", "2026-07-15"), "mid_june_to_mid_july.xlsx"
            )

        reports = db.list_reports(conn)
    assert len(reports) == 1
    assert reports.iloc[0]["source_filename"] == "june.xlsx"


def test_non_overlapping_reports_import_normally(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")
        result = db.import_report(conn, _df("A"), _meta("2026-07-01", "2026-07-31"), "july.xlsx")
        reports = db.list_reports(conn)
    assert result.superseded_report_ids == []
    assert len(reports) == 2


def test_exact_duplicate_period_still_requires_replace_and_is_unaffected_by_overlap_logic(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")

        with pytest.raises(ValueError, match="already"):
            db.import_report(conn, _df("A", "B"), _meta("2026-06-01", "2026-06-30"), "june_v2.xlsx")

        result = db.import_report(
            conn, _df("A", "B"), _meta("2026-06-01", "2026-06-30"), "june_v2.xlsx", replace=True
        )
        reports = db.list_reports(conn)
    assert result.replaced is True
    assert result.superseded_report_ids == []
    assert len(reports) == 1


def test_find_superseded_reports_excludes_the_exact_match(tmp_path):
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")
        superseded, blocking = db.find_superseded_reports(conn, _meta("2026-06-01", "2026-06-30"))
    assert superseded == []
    assert blocking == []
