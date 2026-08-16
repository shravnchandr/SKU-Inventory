"""Regression coverage for the legacy binary `.xls` format.

Every other parser test in this suite builds its fixtures with
`openpyxl.Workbook()`, which can only produce `.xlsx`. But `.xls` is the
*primary* documented input format (README: "Stock Statement .xls exports
your pharmacy software produces"; `sync.py`'s `SUPPORTED_SUFFIXES = {".xls",
".xlsx"}`; the upload validator in `webapp.py` accepts it) — and it goes
through a completely different pandas engine (`xlrd`, not `openpyxl`).
Before this test, that engine had zero direct coverage: a break in the
`.xls` reading path (e.g. an `xlrd` version bump that changes its `read_excel`
behavior) would only surface in production, against a real user's file, not
in CI. Flagged by Agent 3's dependency audit; added here as Agent 4's
highest-priority missing-coverage fix.

Uses `xlwt` (a *test-only* dev dependency, not a runtime dependency of the
app) purely to author the binary `.xls` fixture — the module under test only
ever reads `.xls` files via `pandas.read_excel` -> `xlrd`, never writes them.
"""

import xlwt

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


def _write_stock_statement_xls(path, brand_rows, company="TEST PHARMACY", location="TEST CITY"):
    """Same layout as tests/test_stock_statement_duplicate_skus.py's fixture
    builder, but written as a genuine binary .xls via xlwt instead of .xlsx
    via openpyxl.
    """
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    row_idx = 0

    def _append(cells):
        nonlocal row_idx
        for col, value in enumerate(cells):
            if value is not None:
                ws.write(row_idx, col, value)
        row_idx += 1

    _append([company])
    _append([location])
    _append(["Stock Statement from 01/Jun/2026 to 30/Jun/2026"])
    _append(HEADER_ROW)
    for brand, items in brand_rows:
        _append([brand])
        for item in items:
            _append(_item_row(*item))
        _append(["Sub Total"])
        _append([])
    wb.save(str(path))


def test_parses_a_real_xls_file_via_the_xlrd_engine(tmp_path):
    path = tmp_path / "stock.xls"
    _write_stock_statement_xls(
        path,
        [("BRAND A", [("SKU ONE", 10, 5, 3, 12, 120.0), ("SKU TWO", 20, 0, 10, 10, 200.0)])],
    )
    df, meta = parse_stock_statement(str(path))
    assert list(df["sku"]) == ["SKU ONE", "SKU TWO"]
    assert meta.company == "TEST PHARMACY"
    assert meta.location == "TEST CITY"
    assert meta.period_start.isoformat() == "2026-06-01"
    assert meta.period_end.isoformat() == "2026-06-30"
    row_one = df[df["sku"] == "SKU ONE"].iloc[0]
    assert row_one["opening_stock"] == 10
    assert row_one["purchase"] == 5
    assert row_one["sales"] == 3
    assert row_one["closing_stock"] == 12
    assert row_one["value"] == 120.0


def test_duplicate_skus_within_a_real_xls_file_are_still_summed(tmp_path):
    # Same duplicate-row-summing behavior verified for .xlsx in
    # test_stock_statement_duplicate_skus.py, re-verified here against the
    # .xls/xlrd path specifically — the summing logic runs on the parsed
    # DataFrame regardless of source format, but the *numeric dtypes* xlrd
    # hands back for a legacy file are worth confirming behave the same way.
    path = tmp_path / "stock.xls"
    _write_stock_statement_xls(
        path,
        [
            (
                "BRAND A",
                [
                    ("DUPLICATED SKU", 10, 5, 3, 12, 120.0),
                    ("DUPLICATED SKU", 4, 1, 2, 3, 30.0),
                ],
            )
        ],
    )
    df, _ = parse_stock_statement(str(path))
    assert list(df["sku"]) == ["DUPLICATED SKU"]
    row = df.iloc[0]
    assert row["opening_stock"] == 14
    assert row["closing_stock"] == 15
    assert row["value"] == 150.0
