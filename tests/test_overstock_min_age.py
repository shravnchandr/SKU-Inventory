"""Overstock needs the product to have been around for overstock_days.

"Over N days of cover" means capital tied up only once a product has had
N days to sell; a product first stocked a month ago with few or no sales
yet was landing in Overstock purely for being new (170 of the 253
no-sales overstock items in real data had first appeared within 90 days).
"""

import pandas as pd

from src.gowri_proj.analysis import _status, summarize_history

MONTHS = [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"),
          ("2026-04-01", "2026-04-30")]  # fmt: skip


def _row(month, sku, opening, closing, sales=0.0, purchase=0.0):
    start, end = MONTHS[month]
    return {
        "report_id": month + 1, "period_start": pd.Timestamp(start), "period_end": pd.Timestamp(end),
        "period_days": (pd.Timestamp(end) - pd.Timestamp(start)).days + 1, "company": "T", "location": "T",
        "brand": "B", "sku": sku, "opening_stock": opening, "purchase": purchase, "purchase_free": 0.0,
        "other_receipt": 0.0, "sales": sales, "sales_free": 0.0, "other_issue": 0.0,
        "closing_stock": closing, "value": closing * 10.0,
    }  # fmt: skip


def _status_of(rows, overstock_days=45):
    e = pd.DataFrame(rows)
    s = summarize_history(e, trailing_days_target=90, dead_stock_days=90, low_stock_days=15,
                          overstock_days=overstock_days)  # fmt: skip
    return s.enriched.set_index("sku")["status"].to_dict()


def _filler(month):
    # Keeps every month present so the report set is the same across tests.
    return _row(month, "FILLER", 10, 10, sales=10, purchase=10)


def test_new_product_with_huge_cover_is_not_overstock_yet():
    # First bought in April (the latest month): 30 days old, 100 units, 1 sold.
    rows = [_filler(m) for m in range(4)] + [_row(3, "NEW", 0, 99, sales=1, purchase=100)]
    assert _status_of(rows)["NEW"] == "healthy"


def test_same_product_becomes_overstock_once_old_enough():
    # First bought in January: by the end of April it's ~89 days old.
    rows = [_filler(m) for m in range(4)] + [
        _row(0, "OLDER", 0, 100, purchase=100),
        _row(1, "OLDER", 100, 100), _row(2, "OLDER", 100, 100), _row(3, "OLDER", 100, 99, sales=1),
    ]  # fmt: skip
    assert _status_of(rows)["OLDER"] == "overstock"


def test_age_threshold_follows_the_overstock_setting():
    # Feb-first product is 59 days old at the end of April: too new at a
    # 90-day overstock setting, old enough at 45.
    rows = [_filler(m) for m in range(4)] + [
        _row(1, "FEB", 0, 100, purchase=100), _row(2, "FEB", 100, 100), _row(3, "FEB", 100, 99, sales=1),
    ]  # fmt: skip
    assert _status_of(rows, overstock_days=45)["FEB"] == "overstock"
    assert _status_of(rows, overstock_days=90)["FEB"] == "healthy"


def test_product_already_there_when_tracking_began_is_not_treated_as_new():
    # In the earliest imported report, so it was on the shelf before — even
    # with only one month imported, it can be overstock.
    rows = [_row(0, "BASELINE", 100, 100), _row(0, "SELLER", 10, 10, sales=10, purchase=10)]
    assert _status_of(rows)["BASELINE"] == "overstock"


def test_product_arriving_with_opening_stock_is_not_treated_as_new():
    # First appears in April but *opens* with stock: it was carried in
    # (e.g. under an earlier name), not newly bought.
    rows = [_filler(m) for m in range(4)] + [_row(3, "CARRIED", 100, 99, sales=1)]
    assert _status_of(rows)["CARRIED"] == "overstock"


def test_too_new_never_hides_low_stock_or_dead_stock():
    assert (
        _status(5, False, 3, low_stock_days=15, overstock_days=45, is_too_new=True) == "low_stock"
    )
    assert (
        _status(5, True, 999, low_stock_days=15, overstock_days=45, is_too_new=True) == "dead_stock"
    )
    assert (
        _status(5, False, 999, low_stock_days=15, overstock_days=45, is_too_new=False)
        == "overstock"
    )


def test_too_new_product_still_counts_as_slow_in_value_segments():
    # Healthy in the action lists (too new for Overstock), but with 99 days
    # of cover it isn't a fast mover — value segments describe pace.
    rows = [_filler(m) for m in range(4)] + [_row(3, "NEW", 0, 99, sales=1, purchase=100)]
    s = summarize_history(pd.DataFrame(rows), trailing_days_target=90, dead_stock_days=90,
                          low_stock_days=15, overstock_days=45)  # fmt: skip
    seg = s.value_segment_skus.set_index("sku")
    assert s.enriched.set_index("sku").loc["NEW", "status"] == "healthy"
    assert seg.loc["NEW", "movement"] == "slow"
