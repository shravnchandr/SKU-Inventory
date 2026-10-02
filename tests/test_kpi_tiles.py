"""The Dashboard's KPI tiles: "▲/▼ … vs <last month>" comes from
build_payload's kpis_previous — the last report ending in an earlier
calendar month — and must use the very same figures the tiles show."""

import pandas as pd

from src.gowri_proj.analysis import summarize_history
from src.gowri_proj.dashboard import build_payload


def _rows(rid, start, end, items):
    return [
        {
            "report_id": rid, "period_start": pd.Timestamp(start), "period_end": pd.Timestamp(end),
            "period_days": (pd.Timestamp(end) - pd.Timestamp(start)).days + 1, "company": "T",
            "location": "T", "brand": brand, "sku": sku, "opening_stock": closing, "purchase": 0.0,
            "purchase_free": 0.0, "other_receipt": 0.0, "sales": 1.0, "sales_free": 0.0,
            "other_issue": 0.0, "closing_stock": closing, "value": value,
        }
        for brand, sku, closing, value in items
    ]  # fmt: skip


def _payload(*reports):
    rows = [r for rid, rep in enumerate(reports, 1) for r in _rows(rid, *rep)]
    return build_payload(summarize_history(pd.DataFrame(rows)))


def test_previous_month_figures():
    p = _payload(
        ("2026-06-01", "2026-06-30", [("A", "X", 10, 100.0), ("B", "Y", 5, 50.0)]),
        (
            "2026-07-01",
            "2026-07-31",
            [("A", "X", 12, 120.0), ("A", "Z", 1, 10.0), ("C", "W", 3, 30.0)],
        ),
    )
    assert p["kpis_previous"] == {
        "label": "Jun 2026", "period_end": "2026-06-30", "total_skus": 2, "total_brands": 2,
        "total_value": 150.0, "total_units": 15.0,
    }  # fmt: skip
    assert p["kpis"]["total_skus"] == 3 and p["kpis"]["total_value"] == 160.0


def test_same_month_reports_compare_against_the_previous_month_not_each_other():
    # Two August reports (e.g. a mid-month one, then month-end): "vs last
    # month" means July, not the Aug 1-17 report.
    p = _payload(
        ("2026-07-01", "2026-07-31", [("A", "X", 10, 100.0)]),
        ("2026-08-01", "2026-08-17", [("A", "X", 11, 110.0)]),
        ("2026-08-18", "2026-08-31", [("A", "X", 12, 120.0)]),
    )
    assert p["kpis_previous"]["label"] == "Jul 2026"
    assert p["kpis_previous"]["total_value"] == 100.0


def test_only_one_month_has_no_comparison():
    p = _payload(("2026-07-01", "2026-07-31", [("A", "X", 10, 100.0)]))
    assert p["kpis_previous"] is None


def test_latest_per_report_totals_equal_the_tiles():
    p = _payload(
        ("2026-06-01", "2026-06-30", [("A", "X", 10, 100.0)]),
        ("2026-07-01", "2026-07-31", [("A", "X", 12, 120.5), ("B", "Y", 3, 30.25)]),
    )
    t = p["trend"]
    s = summarize_history(pd.DataFrame(
        _rows(1, "2026-06-01", "2026-06-30", [("A", "X", 10, 100.0)])
        + _rows(2, "2026-07-01", "2026-07-31", [("A", "X", 12, 120.5), ("B", "Y", 3, 30.25)])
    ))  # fmt: skip
    assert s.trend.units_on_hand[-1] == p["kpis"]["total_units"]
    assert s.trend.sku_counts[-1] == p["kpis"]["total_skus"]
    assert s.trend.brand_counts[-1] == p["kpis"]["total_brands"]
    assert t["inventory_value"][-1] == p["kpis"]["total_value"]
