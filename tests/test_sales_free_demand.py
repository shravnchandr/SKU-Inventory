"""Regression test: sales_free (scheme/free-goods units) counts toward
demand for days_of_cover/status, not just paid sales.

Before this fix, a SKU moving only through free/scheme units (paid
sales == 0) would look like it wasn't selling at all — misleadingly
overstock/dead-stock rather than reflecting its true depletion rate. Paid
sales alone still drives revenue-facing figures (top_skus_by_sales,
trend units_sold) — this only changes the demand-rate classification.
"""
import pandas as pd

from src.gowri_proj.analysis import summarize_history


def _row(report_id, period_start, period_end, sku, *, sales=0.0, sales_free=0.0, other_issue=0.0,
         closing_stock=100.0, opening_stock=100.0):
    return {
        "report_id": report_id,
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "period_days": (pd.Timestamp(period_end) - pd.Timestamp(period_start)).days + 1,
        "company": "TEST PHARMACY",
        "location": "TEST CITY",
        "brand": "BRAND",
        "sku": sku,
        "opening_stock": opening_stock,
        "purchase": 0.0,
        "purchase_free": 0.0,
        "other_receipt": 0.0,
        "sales": sales,
        "sales_free": sales_free,
        "other_issue": other_issue,
        "closing_stock": closing_stock,
        "value": closing_stock * 10.0,
    }


def _entries(rows):
    return pd.DataFrame(rows)


def test_days_of_cover_reflects_paid_plus_free_sales():
    # 30 units/month via paid sales alone vs. the same 30 units split
    # 15 paid + 15 free should land on the *same* days_of_cover — the
    # split between paid and free shouldn't matter to the depletion rate.
    all_paid = _entries([_row(1, "2026-05-01", "2026-05-31", "ALL PAID", sales=30, closing_stock=60)])
    half_free = _entries([_row(1, "2026-05-01", "2026-05-31", "HALF FREE", sales=15, sales_free=15, closing_stock=60)])

    cover_all_paid = summarize_history(all_paid, trailing_days_target=31).enriched.iloc[0]["days_of_cover"]
    cover_half_free = summarize_history(half_free, trailing_days_target=31).enriched.iloc[0]["days_of_cover"]
    assert cover_all_paid == cover_half_free


def test_scheme_only_sku_is_not_flagged_low_stock_or_overstock_differently_than_a_paid_equivalent():
    entries = _entries(
        [
            _row(1, "2026-05-01", "2026-05-31", "PAID ONLY", sales=100, closing_stock=10),
            _row(1, "2026-05-01", "2026-05-31", "FREE ONLY", sales=0, sales_free=100, closing_stock=10),
        ]
    )
    summary = summarize_history(entries, trailing_days_target=31, low_stock_days=15)
    statuses = dict(zip(summary.enriched["sku"], summary.enriched["status"]))
    # Both move 100 units/month against 10 on hand — both should be low_stock,
    # not just the paid one.
    assert statuses["PAID ONLY"] == "low_stock"
    assert statuses["FREE ONLY"] == "low_stock"


def test_paid_sales_alone_still_drives_the_displayed_sold_figure():
    # The demand *rate* includes sales_free now, but the "Sold" figure shown
    # in tables/leaderboards must stay paid-only — this is a revenue number,
    # not a depletion-rate number.
    entries = _entries([_row(1, "2026-05-01", "2026-05-31", "SKU", sales=20, sales_free=80, closing_stock=10)])
    summary = summarize_history(entries, trailing_days_target=31)
    assert summary.enriched.iloc[0]["sales"] == 20


def test_is_returned_requires_zero_demand_from_either_paid_or_free_sales():
    # Some free/scheme activity happened, even though paid sales and
    # closing stock are both zero — this genuinely moved, it wasn't just
    # sent back/written off, so it should read as out_of_stock, not
    # "returned".
    entries = _entries(
        [_row(1, "2026-05-01", "2026-05-31", "SKU", sales=0, sales_free=5, other_issue=1, closing_stock=0, opening_stock=6)]
    )
    summary = summarize_history(entries, trailing_days_target=31)
    assert summary.enriched.iloc[0]["status"] == "out_of_stock"
