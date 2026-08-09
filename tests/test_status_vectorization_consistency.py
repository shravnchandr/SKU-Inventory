"""Regression test: the vectorized status computation in summarize_history
(np.select over merged["closing_stock"]/["is_dead"]/["days_of_cover"]) must
keep producing exactly what _status() would for the same three inputs.

_status() itself is no longer called from the production path — it was
replaced by np.select for performance once SKU counts grew large enough
that a row-wise .apply() became the hot spot (see analysis.py's comment
next to the np.select call). It's kept around as a small, readable
reference implementation and exercised directly in test_status.py, but
nothing enforces that the two stay in sync except a comment asking anyone
who touches one to update the other. This test is the actual enforcement:
it runs real data through summarize_history() and checks every resulting
row's status against _status() computed independently from that same row's
closing_stock/is_dead/days_of_cover — if the vectorized logic and the
if/elif chain ever drift, this fails.
"""
import pandas as pd

from src.gowri_proj.analysis import _status, summarize_history

LOW_STOCK_DAYS = 15
OVERSTOCK_DAYS = 90
DEAD_STOCK_DAYS = 30
TRAILING_DAYS_TARGET = 60


def _row(
    report_id, period_start, period_end, sku, *,
    sales=0.0, purchase=0.0, other_issue=0.0, closing_stock=0.0, value=100.0,
):
    return {
        "report_id": report_id,
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "period_days": (pd.Timestamp(period_end) - pd.Timestamp(period_start)).days + 1,
        "company": "TEST PHARMACY",
        "location": "TEST CITY",
        "brand": "TEST BRAND",
        "sku": sku,
        "opening_stock": closing_stock,
        "purchase": purchase,
        "purchase_free": 0.0,
        "other_receipt": 0.0,
        "sales": sales,
        "sales_free": 0.0,
        "other_issue": other_issue,
        "closing_stock": closing_stock,
        "value": value,
    }


def _build_entries() -> pd.DataFrame:
    # One SKU deliberately landing in each of the 5 buckets, across two
    # reports (so there's a real trailing window to average sales over).
    rows = [
        # OUT_OF_STOCK: zero on hand, regardless of how well it's selling.
        _row(1, "2026-06-01", "2026-06-30", "OUT_OF_STOCK", sales=20, closing_stock=5),
        _row(2, "2026-07-01", "2026-07-31", "OUT_OF_STOCK", sales=20, closing_stock=0),
        # RETURNED: zero on hand too, but nothing sold — stock left via a
        # return/write-off (other_issue), not a sale. A different signal
        # from OUT_OF_STOCK even though both end at closing_stock=0.
        _row(1, "2026-06-01", "2026-06-30", "RETURNED", closing_stock=10),
        _row(2, "2026-07-01", "2026-07-31", "RETURNED", other_issue=10, closing_stock=0),
        # DEAD: never purchased or sold, either period — dead since first tracked.
        _row(1, "2026-06-01", "2026-06-30", "DEAD", closing_stock=10),
        _row(2, "2026-07-01", "2026-07-31", "DEAD", closing_stock=10),
        # LOW_STOCK: selling fast relative to what's on hand.
        _row(1, "2026-06-01", "2026-06-30", "LOW_STOCK", sales=120, closing_stock=8),
        _row(2, "2026-07-01", "2026-07-31", "LOW_STOCK", sales=130, closing_stock=5),
        # OVERSTOCK: barely selling, huge pile on hand.
        _row(1, "2026-06-01", "2026-06-30", "OVERSTOCK", sales=3, closing_stock=1000),
        _row(2, "2026-07-01", "2026-07-31", "OVERSTOCK", sales=3, closing_stock=1000),
        # HEALTHY: comfortable middle ground.
        _row(1, "2026-06-01", "2026-06-30", "HEALTHY", sales=50, closing_stock=80),
        _row(2, "2026-07-01", "2026-07-31", "HEALTHY", sales=50, closing_stock=80),
    ]
    return pd.DataFrame(rows)


def test_vectorized_status_matches_status_function_for_every_row():
    entries = _build_entries()
    summary = summarize_history(
        entries,
        trailing_days_target=TRAILING_DAYS_TARGET,
        dead_stock_days=DEAD_STOCK_DAYS,
        low_stock_days=LOW_STOCK_DAYS,
        overstock_days=OVERSTOCK_DAYS,
    )
    assert len(summary.enriched) == 6  # sanity: one row per SKU, not per (report, SKU)

    for _, row in summary.enriched.iterrows():
        expected = _status(
            row["closing_stock"], row["is_dead"], row["days_of_cover"], row["is_returned"],
            LOW_STOCK_DAYS, OVERSTOCK_DAYS,
        )
        assert row["status"] == expected, f"{row['sku']}: vectorized={row['status']!r} but _status()={expected!r}"

    # Also confirm each SKU actually landed in the bucket the test data was
    # built for — a passing per-row check above would be hollow if every
    # SKU had quietly ended up "healthy" some other way.
    by_sku = summary.enriched.set_index("sku")["status"].to_dict()
    assert by_sku == {
        "OUT_OF_STOCK": "out_of_stock",
        "RETURNED": "returned",
        "DEAD": "dead_stock",
        "LOW_STOCK": "low_stock",
        "OVERSTOCK": "overstock",
        "HEALTHY": "healthy",
    }
