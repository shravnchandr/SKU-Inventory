"""HistoryMeta.trailing_window_gaps: a gap *within the specific reports the
current sales-pace figure is computed from* (a narrower question than
Reports/Import health's whole-history gap check). A gap here means
daily_demand divides real sales by a day count that includes untracked
days, silently understating the rate — worth flagging right on the
Dashboard where that number is shown, not just discoverable elsewhere.
"""
import pandas as pd

from src.gowri_proj.analysis import summarize_history


def _row(report_id, period_start, period_end, sku="A", *, sales=1.0, closing_stock=10.0):
    return {
        "report_id": report_id,
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "period_days": (pd.Timestamp(period_end) - pd.Timestamp(period_start)).days + 1,
        "company": "TEST PHARMACY",
        "location": "TEST CITY",
        "brand": "BRAND",
        "sku": sku,
        "opening_stock": closing_stock,
        "purchase": 0.0,
        "purchase_free": 0.0,
        "other_receipt": 0.0,
        "sales": sales,
        "sales_free": 0.0,
        "other_issue": 0.0,
        "closing_stock": closing_stock,
        "value": 100.0,
    }


def test_contiguous_trailing_reports_have_no_gap():
    entries = pd.DataFrame(
        [
            _row(1, "2026-05-01", "2026-05-31"),
            _row(2, "2026-06-01", "2026-06-30"),
        ]
    )
    summary = summarize_history(entries, trailing_days_target=60)
    assert summary.meta.trailing_window_gaps == []


def test_a_gap_between_reports_inside_the_trailing_window_is_reported():
    # A financial-year-rollover-style boundary gap: May ends 5/31, but June
    # doesn't pick up until 6/10 — 9 untracked days in between.
    entries = pd.DataFrame(
        [
            _row(1, "2026-05-01", "2026-05-31"),
            _row(2, "2026-06-10", "2026-06-30"),
        ]
    )
    summary = summarize_history(entries, trailing_days_target=60)
    assert len(summary.meta.trailing_window_gaps) == 1
    gap = summary.meta.trailing_window_gaps[0]
    assert gap["gap_start"] == "2026-06-01"
    assert gap["gap_end"] == "2026-06-09"
    assert gap["days"] == 9


def test_a_gap_outside_the_trailing_window_is_not_reported():
    # The gap is between report 1 and 2, but a narrow trailing_days_target
    # only pulls in report 3 (the latest) — report 1 never enters the
    # trailing window, so its gap with report 2 shouldn't count here (it's
    # still visible on Reports/Import health's full-history check, just not
    # this narrower one).
    entries = pd.DataFrame(
        [
            _row(1, "2026-01-01", "2026-01-31"),
            _row(2, "2026-03-01", "2026-03-31"),  # gap vs report 1, but irrelevant here
            _row(3, "2026-06-01", "2026-06-30"),
        ]
    )
    summary = summarize_history(entries, trailing_days_target=10)
    assert summary.meta.trailing_window_gaps == []
