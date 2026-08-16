"""Regression test: two reports sharing the same period_end must still sort
deterministically as "latest"/"previous", tiebroken on period_start.

The schema only enforces uniqueness on the (period_start, period_end) pair,
not on period_end alone (e.g. a wide baseline import and a monthly report
can both end on the same date), and all_entries is fetched unordered from
SQL. Before the fix, summarize_history's own latest-report pick was already
tiebroken this way, but _select_trailing_reports (the trailing window) and
find_sku_churn's report-pair pick were not — so a same-period_end pair could
disagree with summarize_history about which report is "latest", leaving the
actual latest report out of the trailing window entirely (current -> empty,
total_skus -> 0) or comparing the wrong "latest two" reports for churn.
"""

import pandas as pd

from src.gowri_proj.analysis import find_sku_churn, summarize_history

WIDE_SKU = "OLD BASELINE SKU"
NEW_SKU = "ACTUAL LATEST SKU"


def _row(report_id, period_start, period_end, sku, *, sales=1.0, closing_stock=10.0, value=100.0):
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
        "value": value,
    }


def test_summarize_history_picks_the_report_with_the_later_start_when_end_dates_tie():
    # Report 1: a wide baseline spanning a whole year, ending 2026-07-31.
    # Report 2: a normal monthly report, also ending 2026-07-31, but
    # starting later — this is the actual "latest" report.
    entries = pd.DataFrame(
        [
            _row(1, "2025-08-01", "2026-07-31", WIDE_SKU),
            _row(2, "2026-07-01", "2026-07-31", NEW_SKU),
        ]
    )
    summary = summarize_history(entries)
    assert summary.total_skus == 1
    assert list(summary.enriched["sku"]) == [NEW_SKU]


def test_find_sku_churn_picks_the_same_latest_report_as_summarize_history():
    # Report 2 (wide baseline) and report 3 both end 2026-07-31; report 3
    # starts later, so it's the actual latest report — same as
    # summarize_history determines above. find_sku_churn must agree, or it
    # ends up comparing the wrong "latest two" reports and could surface
    # false new/vanished SKUs.
    entries = pd.DataFrame(
        [
            _row(2, "2025-08-01", "2026-07-31", WIDE_SKU),
            _row(3, "2026-07-01", "2026-07-31", NEW_SKU),
        ]
    )
    result = find_sku_churn(entries)
    assert result["previous_period_end"] == "2026-07-31"
    assert [r["sku"] for r in result["new_skus"]] == [NEW_SKU]
    assert [r["sku"] for r in result["vanished_skus"]] == [WIDE_SKU]
