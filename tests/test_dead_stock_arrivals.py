"""Dead stock's "last restocked" counts any stock that actually arrived —
paid purchase, free/scheme units, or a net transfer in — not just paid
purchases. A same-period swap (other_receipt matched by other_issue, e.g.
an expiry exchange) leaves the same old stock on the shelf and must not
reset the clock. On real data: 13 of 40 flagged items had a real arrival;
the other 27 were swaps and stay dead stock.
"""

import pandas as pd
import pytest

from src.gowri_proj.analysis import summarize_history

MONTHS = [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"),
          ("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-31")]  # fmt: skip


def _row(month, sku, opening, closing, **kw):
    start, end = MONTHS[month]
    base = {
        "report_id": month + 1, "period_start": pd.Timestamp(start), "period_end": pd.Timestamp(end),
        "period_days": (pd.Timestamp(end) - pd.Timestamp(start)).days + 1, "company": "T", "location": "T",
        "brand": "B", "sku": sku, "opening_stock": opening, "purchase": 0.0, "purchase_free": 0.0,
        "other_receipt": 0.0, "sales": 0.0, "sales_free": 0.0, "other_issue": 0.0,
        "closing_stock": closing, "value": closing * 10.0,
    }  # fmt: skip
    base.update(kw)
    return base


def _status(last_month_changes):
    # Sat untouched with 10 units Jan-Apr (well past 90 days by May),
    # then whatever happens in May.
    rows = [_row(m, "X", 10, 10) for m in range(4)]
    rows.append(_row(4, "X", 10, last_month_changes.pop("closing"), **last_month_changes))
    rows += [_row(m, "SELLER", 10, 10, sales=10, purchase=10) for m in range(5)]
    s = summarize_history(pd.DataFrame(rows), trailing_days_target=90, dead_stock_days=90)
    return s.enriched.set_index("sku").loc["X", "status"]


def test_untouched_is_dead():
    assert _status({"closing": 10}) == "dead_stock"


@pytest.mark.parametrize(
    "change",
    [
        {"purchase": 20, "closing": 30},  # paid purchase (unchanged behaviour)
        {"purchase_free": 20, "closing": 30},  # free/scheme units only
        {"other_receipt": 20, "closing": 30},  # transfer in
        {"other_receipt": 20, "other_issue": 5, "closing": 25},  # net +15
    ],
)
def test_real_arrival_resets_the_clock(change):
    assert _status(change) != "dead_stock"


@pytest.mark.parametrize(
    "change",
    [
        {"other_receipt": 5, "other_issue": 5, "closing": 10},  # exchange: same 5 in and out
        {"other_receipt": 5, "other_issue": 8, "closing": 7},  # more went out than came in
    ],
)
def test_swap_does_not_reset_the_clock(change):
    assert _status(change) == "dead_stock"
