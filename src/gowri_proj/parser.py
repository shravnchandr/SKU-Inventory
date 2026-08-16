"""Parse the pharmacy "Stock Statement" .xls export into a tidy DataFrame.

The source report is a printed-style export: a brand/category name on its own
row, followed by one row per SKU under that brand, followed by a "Sub Total"
row, a blank separator row, then the next brand. This module walks the raw
sheet and reshapes it into one row per SKU.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class ReportMeta:
    company: str | None
    location: str | None
    period_start: date | None
    period_end: date | None

    @property
    def period_days(self) -> int:
        if self.period_start and self.period_end:
            return (self.period_end - self.period_start).days + 1
        return 122  # fallback: ~4 months


# Column positions in the raw, headerless sheet.
COL_NAME = 0
COL_OPENING = 5
COL_PURCHASE = 6
COL_PURCHASE_FREE = 7
COL_OTHER_RECEIPT = 8
COL_SALES = 9
COL_SALES_FREE = 10
COL_OTHER_ISSUE = 11
COL_CLOSING_STOCK = 13
COL_VALUE = 14

NUMERIC_COLS = [
    COL_OPENING,
    COL_PURCHASE,
    COL_PURCHASE_FREE,
    COL_OTHER_RECEIPT,
    COL_SALES,
    COL_SALES_FREE,
    COL_OTHER_ISSUE,
    COL_CLOSING_STOCK,
    COL_VALUE,
]

TIDY_COLUMNS = [
    "brand",
    "sku",
    "opening_stock",
    "purchase",
    "purchase_free",
    "other_receipt",
    "sales",
    "sales_free",
    "other_issue",
    "closing_stock",
    "value",
]


def load_raw(path: str) -> pd.DataFrame:
    """Read the .xls report as a headerless grid of raw cells."""
    return pd.read_excel(path, header=None)


_PERIOD_RE = re.compile(r"Stock Statement from (\d{2}/\w{3}/\d{4}) to (\d{2}/\w{3}/\d{4})")


def _scan_banner(
    raw: pd.DataFrame, pattern: re.Pattern
) -> tuple[str | None, str | None, re.Match | None]:
    """Walk the first 10 rows of a raw export looking for the company name,
    location, and the first regex match against `pattern` — the shared shape
    of both banner layouts this app reads (stock statement, item list): a
    company name line, then either a location line or the line `pattern`
    matches (order between those two isn't fixed), each in its own row.
    Returns (company, location, match) — `match` is None if `pattern` never
    matched within the first 10 rows.
    """
    company = None
    location = None
    matched = None

    for _, row in raw.head(10).iterrows():
        # Banner rows center their text in a merged cell, not always column 0.
        texts = [v.strip() for v in row if isinstance(v, str) and v.strip()]
        if not texts:
            continue
        text = texts[0]
        if company is None:
            company = text
            continue
        match = pattern.search(text)
        if match:
            matched = match
        elif location is None:
            location = text

    return company, location, matched


def parse_meta(raw: pd.DataFrame) -> ReportMeta:
    """Pull the company name, location, and reporting period off the banner rows."""
    company, location, match = _scan_banner(raw, _PERIOD_RE)
    period_start = pd.to_datetime(match.group(1), format="%d/%b/%Y").date() if match else None
    period_end = pd.to_datetime(match.group(2), format="%d/%b/%Y").date() if match else None
    return ReportMeta(company, location, period_start, period_end)


@dataclass(frozen=True)
class ItemListMeta:
    company: str | None
    location: str | None
    as_of: date | None


# Column positions in the raw "item list" sheet (a different export than the
# stock statement — same headerless-grid read, different layout).
IL_COL_CODE = 0
IL_COL_PRODUCT = 1
IL_COL_PACKING = 2
IL_COL_MRP = 3
IL_COL_BY_RATE = 4
IL_COL_TAX_PCT = 5
# Column 6 (Sp.Rate) is skipped — not read into product_name.
IL_COL_HSN = 7
IL_COL_LONG_NAME = 8

ITEM_LIST_COLUMNS = [
    "code",
    "brand",
    "product_name",
    "packing",
    "mrp",
    "by_rate",
    "tax_pct",
    "hsn",
    "long_name",
]

_ITEM_LIST_AS_OF_RE = re.compile(r"Item List as on (\d{2}/\d{2}/\d{4})")


def parse_item_list_meta(raw: pd.DataFrame) -> ItemListMeta:
    """Pull the company name, location, and "as on" date off the banner rows."""
    company, location, match = _scan_banner(raw, _ITEM_LIST_AS_OF_RE)
    as_of = pd.to_datetime(match.group(1), format="%d/%m/%Y").date() if match else None
    return ItemListMeta(company, location, as_of)


def parse_item_list(path: str, sheet_name: str = "Sheet2") -> tuple[pd.DataFrame, ItemListMeta]:
    """Parse the "item list" POS export into a tidy, one-row-per-code DataFrame.

    Structurally similar to the stock statement (brand-header rows interspersed
    with item rows) but a different shape entirely — this maps a stable internal
    `code` to the *current* product name/brand/HSN for every item ever set up in
    the system, which is what lets renamed/re-labeled SKUs in the monthly stock
    statements be flagged rather than silently mismatched.
    """
    raw = pd.read_excel(path, sheet_name=sheet_name, header=None)
    meta = parse_item_list_meta(raw)

    rows: list[dict] = []
    current_brand: str | None = None
    started = False

    for _, row in raw.iterrows():
        code = row[IL_COL_CODE]
        has_code = code.strip() != "" if isinstance(code, str) else pd.notna(code)

        if not started:
            if str(code).strip() == "Code":
                # As with parse_stock_statement's header check: verify a
                # couple more cells are where expected before trusting fixed
                # column positions for every row after this one.
                if (
                    str(row[IL_COL_PRODUCT]).strip() != "Product"
                    or str(row[IL_COL_HSN]).strip() != "HSN"
                ):
                    raise ValueError(
                        "Found the 'Code' header but the other columns don't match the "
                        f"expected layout (expected 'Product' in column {IL_COL_PRODUCT + 1} "
                        f"and 'HSN' in column {IL_COL_HSN + 1}, got {row[IL_COL_PRODUCT]!r} "
                        f"and {row[IL_COL_HSN]!r}). The export template may have changed — "
                        "check the file before re-importing."
                    )
                started = True
            continue

        name_cell = row[IL_COL_PRODUCT]
        has_name = isinstance(name_cell, str) and name_cell.strip() != ""
        if not has_code and not has_name:
            continue  # blank separator row

        if not has_name:
            # A brand/category header row: only the code-ish column (the
            # row's sole populated cell) is filled — e.g. "AKSIGEN" on its
            # own row, followed by that brand's items each with a real code
            # *and* a product name. A blank Product cell is what marks it as
            # a header rather than an item, not a blank Code cell (both
            # header and item rows have that column filled).
            current_brand = str(code).strip()
            continue

        rows.append(
            {
                "code": str(code).strip(),
                "brand": current_brand,
                "product_name": str(name_cell).strip(),
                "packing": str(row[IL_COL_PACKING]).strip()
                if isinstance(row[IL_COL_PACKING], str)
                else row[IL_COL_PACKING],
                "mrp": row[IL_COL_MRP],
                "by_rate": row[IL_COL_BY_RATE],
                "tax_pct": row[IL_COL_TAX_PCT],
                "hsn": str(row[IL_COL_HSN]).strip()
                if isinstance(row[IL_COL_HSN], str)
                else row[IL_COL_HSN],
                "long_name": str(row[IL_COL_LONG_NAME]).strip()
                if isinstance(row[IL_COL_LONG_NAME], str)
                else row[IL_COL_LONG_NAME],
            }
        )

    df = pd.DataFrame(rows, columns=ITEM_LIST_COLUMNS)
    numeric_cols = ["mrp", "by_rate", "tax_pct"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    return df, meta


def parse_stock_statement(path: str) -> tuple[pd.DataFrame, ReportMeta]:
    """Parse the raw report into a tidy, one-row-per-SKU DataFrame plus metadata."""
    raw = load_raw(path)
    meta = parse_meta(raw)

    rows: list[dict] = []
    current_brand: str | None = None
    started = False  # becomes True once we've passed the header row

    for _, row in raw.iterrows():
        name = row[COL_NAME]
        numeric_vals = row[NUMERIC_COLS]
        has_name = isinstance(name, str) and name.strip() != ""
        all_numeric_blank = numeric_vals.isna().all()

        if not started:
            if str(row[COL_OPENING]).strip() == "Opening Stock":
                # COL_OPENING matching is necessary but not sufficient — the
                # numeric columns are trusted by fixed position from here on
                # (COL_SALES, COL_VALUE, etc.), so if the export template
                # ever adds/reorders a column downstream of "Opening Stock",
                # this one check would still pass while every number read
                # after it is silently wrong (e.g. a "Sales" figure actually
                # read from a "Free" column). Checking a couple more header
                # cells catches that instead of parsing garbage quietly.
                if str(row[COL_SALES]).strip() != "Sales" or str(row[COL_VALUE]).strip() != "Value":
                    raise ValueError(
                        "Found the 'Opening Stock' header but the other columns don't match the "
                        f"expected layout (expected 'Sales' in column {COL_SALES + 1} and 'Value' in "
                        f"column {COL_VALUE + 1}, got {row[COL_SALES]!r} and {row[COL_VALUE]!r}). "
                        "The export template may have changed — check the file before re-importing, "
                        "since the numbers would otherwise be read from the wrong columns silently."
                    )
                started = True
            continue

        if not has_name:
            continue  # blank separator row

        name = name.strip()

        if name in ("Sub Total", "Grand Total"):
            continue

        if all_numeric_blank:
            # A brand/category header row.
            current_brand = name
            continue

        rows.append(
            {
                "brand": current_brand,
                "sku": name,
                "opening_stock": row[COL_OPENING],
                "purchase": row[COL_PURCHASE],
                "purchase_free": row[COL_PURCHASE_FREE],
                "other_receipt": row[COL_OTHER_RECEIPT],
                "sales": row[COL_SALES],
                "sales_free": row[COL_SALES_FREE],
                "other_issue": row[COL_OTHER_ISSUE],
                "closing_stock": row[COL_CLOSING_STOCK],
                "value": row[COL_VALUE],
            }
        )

    df = pd.DataFrame(rows, columns=TIDY_COLUMNS)
    numeric_cols = TIDY_COLUMNS[2:]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)

    # The same SKU listed twice within one report (a source-export quirk —
    # e.g. a batch split that lost its batch info in this flat export) would
    # otherwise silently double-count that SKU's stock/value: db.py enforces
    # one stock_entries row per (report, sku), and analysis.py's "current
    # snapshot" assumes the same. Collapse duplicates by summing every
    # numeric column rather than rejecting the whole file over it — the
    # totals this produces are the honest combined figures either way, and
    # not importing a real month's data over a formatting quirk elsewhere in
    # the file would be worse. Keep the first brand seen; a genuine SKU
    # identity collision under two different brands within the same report
    # would be a real problem, but distinct from what this is fixing.
    if df["sku"].duplicated().any():
        df = df.groupby("sku", as_index=False, sort=False).agg(
            {"brand": "first", **{col: "sum" for col in numeric_cols}}
        )[TIDY_COLUMNS]

    return df, meta
