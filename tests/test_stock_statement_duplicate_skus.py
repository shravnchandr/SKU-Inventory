"""Regression test: a SKU listed twice within one stock statement (a
source-export quirk — e.g. a batch split that lost its batch info in this
flat export) must be summed into one row, not left as two — db.py enforces
one stock_entries row per (report, sku), and analysis.py's "current
snapshot" assumes the same; two rows for the same SKU would otherwise
silently double-count its stock/value everywhere downstream.
"""

import openpyxl
import pytest

from src.gowri_proj.parser import parse_stock_statement

HEADER_ROW = [
    "Item",
    None,
    None,
    None,
    None,
    "Opening Stock",
    "Purchase",
    "Purchase Free",
    "Other Receipt",
    "Sales",
    "Sales Free",
    "Other Issue",
    None,
    "Closing Stock",
    "Value",
]


def _item_row(sku, opening, purchase, sales, closing, value):
    row = [None] * 15
    row[0] = sku
    row[5] = opening
    row[6] = purchase
    row[9] = sales
    row[13] = closing
    row[14] = value
    return row


def _write_stock_statement(path, brand_rows, company="TEST PHARMACY", location="TEST CITY"):
    """brand_rows: list of (brand, [(sku, opening, purchase, sales, closing, value), ...])."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([company])
    ws.append([location])
    ws.append(["Stock Statement from 01/Jun/2026 to 30/Jun/2026"])
    ws.append(HEADER_ROW)
    for brand, items in brand_rows:
        ws.append([brand] + [None] * 14)
        for item in items:
            ws.append(_item_row(*item))
        ws.append(["Sub Total"] + [None] * 14)
        ws.append([None] * 15)
    wb.save(path)


def test_parses_one_row_per_sku_with_no_duplicates(tmp_path):
    path = tmp_path / "stock.xlsx"
    _write_stock_statement(
        path,
        [("BRAND A", [("SKU ONE", 10, 5, 3, 12, 120.0), ("SKU TWO", 20, 0, 10, 10, 200.0)])],
    )
    df, meta = parse_stock_statement(str(path))
    assert list(df["sku"]) == ["SKU ONE", "SKU TWO"]
    assert meta.period_start.isoformat() == "2026-06-01"


def test_duplicate_sku_rows_within_one_brand_are_summed(tmp_path):
    path = tmp_path / "stock.xlsx"
    _write_stock_statement(
        path,
        [
            (
                "BRAND A",
                [
                    ("DUPLICATED SKU", 10, 5, 3, 12, 120.0),
                    ("DUPLICATED SKU", 4, 1, 2, 3, 30.0),
                    ("STABLE SKU", 20, 0, 10, 10, 200.0),
                ],
            )
        ],
    )
    df, _ = parse_stock_statement(str(path))
    assert sorted(df["sku"]) == ["DUPLICATED SKU", "STABLE SKU"]
    row = df[df["sku"] == "DUPLICATED SKU"].iloc[0]
    assert row["opening_stock"] == 14
    assert row["purchase"] == 6
    assert row["sales"] == 5
    assert row["closing_stock"] == 15
    assert row["value"] == pytest.approx(150.0)
    assert row["brand"] == "BRAND A"


def test_duplicate_sku_rows_across_brand_sections_are_summed(tmp_path):
    # Same SKU name reappearing under a second brand section within the
    # *same* report — a rename only ever happens between reports (a single
    # snapshot in time has one classification), so this is the same
    # duplicate-row problem, not a genuine identity collision. Summed the
    # same way, keeping the first-seen brand.
    path = tmp_path / "stock.xlsx"
    _write_stock_statement(
        path,
        [
            ("BRAND A", [("SHARED SKU", 10, 0, 0, 10, 100.0)]),
            ("BRAND B", [("SHARED SKU", 5, 0, 0, 5, 50.0)]),
        ],
    )
    df, _ = parse_stock_statement(str(path))
    assert list(df["sku"]) == ["SHARED SKU"]
    row = df.iloc[0]
    assert row["brand"] == "BRAND A"
    assert row["opening_stock"] == 15
    assert row["closing_stock"] == 15
    assert row["value"] == pytest.approx(150.0)
