"""Unit tests for analysis._compute_dead_stock.

Covers: the last-purchase-or-sale-whichever-is-later anchor, the bounded
lookback window, and the earliest-report fallback fix (a SKU with zero
activity in its *entire* recorded history must not be aged past however
long it's actually been tracked — see the AGING_LOOKBACK_FLOOR_DAYS /
earliest_period_start handling in analysis.py).
"""

import pandas as pd
import pytest

from src.gowri_proj.analysis import _compute_dead_stock

LATEST = pd.Timestamp("2026-07-31")


def _entries(rows: list[dict]) -> pd.DataFrame:
    """rows: dicts with sku, period_end (str), purchase, sales, sales_free."""
    df = pd.DataFrame(rows)
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["brand"] = df.get("brand", "SOME BRAND")
    for col in ("purchase", "sales", "sales_free"):
        if col not in df.columns:
            df[col] = 0.0
    return df


def _current(skus: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"sku": skus})


def test_recent_purchase_not_dead():
    entries = _entries([{"sku": "A", "period_end": "2026-07-31", "purchase": 10, "sales": 0}])
    result = _compute_dead_stock(
        entries, _current(["A"]), LATEST, pd.Timestamp("2025-01-01"), dead_stock_days=90
    )
    row = result[result["sku"] == "A"].iloc[0]
    assert not row["is_dead"]
    assert row["days_since_activity"] == 0


def test_recent_sale_not_dead():
    entries = _entries([{"sku": "A", "period_end": "2026-06-30", "sales": 5}])
    result = _compute_dead_stock(
        entries, _current(["A"]), LATEST, pd.Timestamp("2025-01-01"), dead_stock_days=90
    )
    row = result[result["sku"] == "A"].iloc[0]
    assert not row["is_dead"]  # 31 days ago, under the 90-day threshold


def test_recent_scheme_sale_with_zero_paid_sales_is_not_dead():
    # A SKU only moving via scheme/free-goods units (sales=0, sales_free>0)
    # is still leaving the shelf — it shouldn't read as dead just because
    # none of its recent activity was a *paid* sale.
    entries = _entries([{"sku": "A", "period_end": "2026-06-30", "sales": 0, "sales_free": 5}])
    result = _compute_dead_stock(
        entries, _current(["A"]), LATEST, pd.Timestamp("2025-01-01"), dead_stock_days=90
    )
    row = result[result["sku"] == "A"].iloc[0]
    assert not row["is_dead"]  # 31 days ago, under the 90-day threshold


def test_anchored_to_whichever_is_more_recent_purchase_or_sale():
    # Sale is more recent than purchase — clock should reset on the sale.
    entries = _entries(
        [
            {"sku": "A", "period_end": "2026-01-31", "purchase": 10, "sales": 0},
            {"sku": "A", "period_end": "2026-06-30", "purchase": 0, "sales": 3},
        ]
    )
    result = _compute_dead_stock(
        entries, _current(["A"]), LATEST, pd.Timestamp("2025-01-01"), dead_stock_days=90
    )
    row = result[result["sku"] == "A"].iloc[0]
    assert row["days_since_activity"] == (LATEST - pd.Timestamp("2026-06-30")).days
    assert not row["is_dead"]


def test_old_purchase_and_sale_is_dead():
    entries = _entries([{"sku": "A", "period_end": "2026-01-31", "purchase": 10, "sales": 2}])
    result = _compute_dead_stock(
        entries, _current(["A"]), LATEST, pd.Timestamp("2025-01-01"), dead_stock_days=90
    )
    row = result[result["sku"] == "A"].iloc[0]
    assert row["is_dead"]


def test_no_activity_ever_falls_back_to_earliest_tracked_date_not_the_lookback_window():
    # Regression test: a fresh install with only ~5 months of history and a
    # SKU that's never once been purchased or sold. The 750-day lookback
    # floor extends the search window before any data exists at all — the
    # fallback must use the earliest report's period_start (honest: "dead
    # since we started tracking it"), not the fictional pre-history cutoff
    # 750 days back, which would fabricate an inflated age the data can't
    # support (e.g. wrongly bucketing it as "2+ years dead").
    earliest = pd.Timestamp("2026-03-01")
    entries = _entries([{"sku": "OTHER", "period_end": "2026-07-31", "purchase": 1, "sales": 1}])
    result = _compute_dead_stock(entries, _current(["A"]), LATEST, earliest, dead_stock_days=90)
    row = result[result["sku"] == "A"].iloc[0]
    assert row["is_dead"]
    expected_days = (LATEST - earliest).days
    assert row["days_since_activity"] == expected_days
    assert row["days_since_activity"] < 730  # must NOT look like a 2+ year old SKU


def test_no_activity_within_a_long_history_uses_the_lookback_window_not_full_history():
    # Opposite case: plenty of real history exists before the lookback
    # window, but nothing in the window itself — this is the case the
    # AGING_LOOKBACK_FLOOR_DAYS optimization exists for. The fallback
    # should be the window's own cutoff (~750 days back), not scan further.
    earliest = pd.Timestamp("2015-01-01")  # over a decade of real history
    entries = _entries([{"sku": "OTHER", "period_end": "2026-07-31", "purchase": 1, "sales": 1}])
    result = _compute_dead_stock(entries, _current(["A"]), LATEST, earliest, dead_stock_days=90)
    row = result[result["sku"] == "A"].iloc[0]
    assert row["is_dead"]
    # Should be pinned near the 750-day lookback floor, not ~4200 days
    # (which is what "all the way back to 2015" would produce).
    assert row["days_since_activity"] == pytest.approx(750, abs=1)
