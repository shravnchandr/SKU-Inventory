"""Unit tests for find_data_quality_issues / _quality_checks.

No existing coverage for this function before now. Focus: the new
stock-flow balance check (opening + purchase + purchase_free + other_receipt
- sales - sales_free - other_issue should equal closing_stock, within a
tolerance for real-world rounding) alongside the pre-existing impossible-
value checks.
"""

import pandas as pd

from src.gowri_proj.analysis import find_data_quality_issues

BASE_ROW = {
    "brand": "BRAND",
    "sku": "SKU",
    "period_start": pd.Timestamp("2026-06-01"),
    "period_end": pd.Timestamp("2026-06-30"),
    "opening_stock": 10.0,
    "purchase": 5.0,
    "purchase_free": 0.0,
    "other_receipt": 0.0,
    "sales": 3.0,
    "sales_free": 0.0,
    "other_issue": 0.0,
    "closing_stock": 12.0,
    "value": 100.0,
}


def _entries(overrides_list):
    rows = [{**BASE_ROW, **overrides} for overrides in overrides_list]
    return pd.DataFrame(rows)


def test_a_balanced_row_is_not_flagged():
    # 10 + 5 - 3 = 12, matches closing_stock exactly.
    entries = _entries([{}])
    assert find_data_quality_issues(entries) == []


def test_a_small_rounding_discrepancy_is_not_flagged():
    entries = _entries([{"closing_stock": 12.3}])  # off by 0.3, within tolerance
    assert find_data_quality_issues(entries) == []


def test_a_real_balance_discrepancy_is_flagged():
    entries = _entries([{"closing_stock": 20.0}])  # expected 12, off by 8
    issues = find_data_quality_issues(entries)
    assert len(issues) == 1
    assert "balance" in issues[0]["issue"]
    assert "-8.0" in issues[0]["issue"]


def test_free_and_other_receipt_issue_columns_count_toward_the_balance():
    # 10 + 5 (paid) + 2 (free) + 1 (other receipt) - 3 (paid) - 1 (free) - 1 (other issue) = 13
    entries = _entries(
        [
            {
                "purchase_free": 2.0,
                "other_receipt": 1.0,
                "sales_free": 1.0,
                "other_issue": 1.0,
                "closing_stock": 13.0,
            }
        ]
    )
    assert find_data_quality_issues(entries) == []


def test_negative_closing_stock_is_still_flagged():
    entries = _entries(
        [{"closing_stock": -1.0, "opening_stock": 0.0, "purchase": 0.0, "sales": 0.0}]
    )
    issues = find_data_quality_issues(entries)
    assert any("Negative closing stock" in i["issue"] for i in issues)


def test_positive_stock_with_negative_value_is_still_flagged():
    entries = _entries([{"value": -50.0}])
    issues = find_data_quality_issues(entries)
    assert any("negative value" in i["issue"] for i in issues)


def test_a_row_can_have_multiple_issues_at_once():
    entries = _entries([{"closing_stock": -5.0, "opening_stock": -1.0}])
    issues = find_data_quality_issues(entries)
    assert len(issues) >= 2
    kinds = {i["issue"] for i in issues}
    assert "Negative closing stock" in kinds
    assert "Negative opening stock" in kinds
