"""Regression test for the brand-rename identity bug.

Reproduces the real incident (a distributor renaming a brand label
mid-history, e.g. "SUN PHARMA CONSUMER" -> "SUN PHARMA SIRIUS") at small
scale: one SKU sold under an old brand label for two months, then under a
new one. Every join in analysis.py must key on sku alone so a rename
doesn't silently split the SKU's history in two — see _compute_dead_stock's
docstring for the full reasoning, and sku_history's.

Before the fix, this exact shape understated trailing sales by dropping
the pre-rename months, pushed days_of_cover up incorrectly, and truncated
sku_history to only the post-rename periods.
"""

import pandas as pd

from src.gowri_proj.analysis import sku_history, summarize_history

RENAMED_SKU = "AZTOR EZ 40MG TAB"
OLD_BRAND = "SUN PHARMA CONSUMER"
NEW_BRAND = "SUN PHARMA SIRIUS"

# A second, unrelated SKU with a stable brand — present so the fix's
# sku-only keying doesn't accidentally cross-contaminate unrelated SKUs.
STABLE_SKU = "PARACETAMOL 500MG"
STABLE_BRAND = "GENERIC PHARMA"


def _row(
    report_id,
    period_start,
    period_end,
    brand,
    sku,
    *,
    sales,
    purchase=0.0,
    closing_stock=100.0,
    value=1000.0,
):
    return {
        "report_id": report_id,
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "period_days": (pd.Timestamp(period_end) - pd.Timestamp(period_start)).days + 1,
        "company": "TEST PHARMACY",
        "location": "TEST CITY",
        "brand": brand,
        "sku": sku,
        "opening_stock": closing_stock,
        "purchase": purchase,
        "purchase_free": 0.0,
        "other_receipt": 0.0,
        "sales": sales,
        "sales_free": 0.0,
        "other_issue": 0.0,
        "closing_stock": closing_stock,
        "value": value,
    }


def _build_entries() -> pd.DataFrame:
    rows = [
        # Renamed SKU: two months under the old brand, one under the new one.
        _row(1, "2026-02-01", "2026-02-28", OLD_BRAND, RENAMED_SKU, sales=30, purchase=100),
        _row(2, "2026-03-01", "2026-03-31", OLD_BRAND, RENAMED_SKU, sales=30),
        _row(3, "2026-04-01", "2026-04-30", NEW_BRAND, RENAMED_SKU, sales=40, closing_stock=100),
        # Stable, unrelated SKU present every period under one brand.
        _row(1, "2026-02-01", "2026-02-28", STABLE_BRAND, STABLE_SKU, sales=5),
        _row(2, "2026-03-01", "2026-03-31", STABLE_BRAND, STABLE_SKU, sales=5),
        _row(3, "2026-04-01", "2026-04-30", STABLE_BRAND, STABLE_SKU, sales=5),
    ]
    return pd.DataFrame(rows)


def test_trailing_sales_span_the_rename():
    entries = _build_entries()
    # Trailing window wide enough to cover all three months.
    summary = summarize_history(entries, trailing_days_target=90)
    row = summary.enriched[summary.enriched["sku"] == RENAMED_SKU].iloc[0]
    # True total across the window: 30 + 30 + 40 = 100, not just the 40
    # sold under the current (new) brand label.
    assert row["sales"] == 100.0


def test_current_display_brand_is_the_latest_label():
    entries = _build_entries()
    summary = summarize_history(entries, trailing_days_target=90)
    row = summary.enriched[summary.enriched["sku"] == RENAMED_SKU].iloc[0]
    assert row["brand"] == NEW_BRAND


def test_sku_history_includes_periods_under_every_brand_label():
    entries = _build_entries()
    history = sku_history(entries, RENAMED_SKU)
    assert len(history) == 3
    assert [h["sales"] for h in history] == [30.0, 30.0, 40.0]


def test_unrelated_sku_with_stable_brand_is_unaffected():
    entries = _build_entries()
    summary = summarize_history(entries, trailing_days_target=90)
    row = summary.enriched[summary.enriched["sku"] == STABLE_SKU].iloc[0]
    assert row["sales"] == 15.0
    assert row["brand"] == STABLE_BRAND


def test_dead_stock_activity_carries_across_the_rename():
    # Purchase happened under the OLD brand label, two months before the
    # (arbitrarily short, for this test) dead-stock threshold — the SKU's
    # dead-stock clock must be anchored to that purchase regardless of
    # which brand label it was recorded under.
    entries = _build_entries()
    summary = summarize_history(entries, trailing_days_target=90, dead_stock_days=90)
    row = summary.enriched[summary.enriched["sku"] == RENAMED_SKU].iloc[0]
    # Latest activity is the April sale (40 units) under the new brand —
    # well within 90 days of the latest period end, so not dead.
    assert not row["is_dead"]
