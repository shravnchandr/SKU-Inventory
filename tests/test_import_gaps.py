"""Unit tests for analysis.find_import_gaps."""

import pandas as pd

from src.gowri_proj.analysis import find_import_gaps


def _reports(periods: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame([{"period_start": s, "period_end": e} for s, e in periods])


def test_empty_reports_has_no_gaps():
    result = find_import_gaps(pd.DataFrame(columns=["period_start", "period_end"]))
    assert result == {"missing_months": [], "coverage_gaps": []}


def test_contiguous_monthly_reports_have_no_gaps():
    reports = _reports(
        [
            ("2026-01-01", "2026-01-31"),
            ("2026-02-01", "2026-02-28"),
            ("2026-03-01", "2026-03-31"),
        ]
    )
    result = find_import_gaps(reports)
    assert result["missing_months"] == []
    assert result["coverage_gaps"] == []


def test_day_level_gap_between_reports_is_caught():
    # This is the shape of the real incident that motivated this function:
    # a boundary gap that doesn't line up with whole calendar months.
    reports = _reports(
        [
            ("2026-01-01", "2026-01-31"),
            ("2026-02-05", "2026-02-28"),  # 4-day gap: Feb 1-4 never imported
        ]
    )
    result = find_import_gaps(reports)
    assert len(result["coverage_gaps"]) == 1
    gap = result["coverage_gaps"][0]
    assert gap["gap_start"] == "2026-02-01"
    assert gap["gap_end"] == "2026-02-04"
    assert gap["days"] == 4


def test_entirely_missing_calendar_month_is_caught():
    reports = _reports(
        [
            ("2026-01-01", "2026-01-31"),
            ("2026-03-01", "2026-03-31"),  # February has zero coverage
        ]
    )
    result = find_import_gaps(reports)
    assert "Feb 2026" in result["missing_months"]
    assert len(result["coverage_gaps"]) == 1


def test_one_wide_report_spanning_months_covers_all_of_them():
    # A wide report (e.g. the very first import spanning several months at
    # once) should count as covering every month it overlaps, not just the
    # one it starts in.
    reports = _reports([("2026-01-01", "2026-03-31")])
    result = find_import_gaps(reports)
    assert result["missing_months"] == []
    assert result["coverage_gaps"] == []


def test_reports_out_of_chronological_order_are_still_handled_correctly():
    reports = _reports(
        [
            ("2026-03-01", "2026-03-31"),
            ("2026-01-01", "2026-01-31"),
            ("2026-02-01", "2026-02-28"),
        ]
    )
    result = find_import_gaps(reports)
    assert result["missing_months"] == []
    assert result["coverage_gaps"] == []
