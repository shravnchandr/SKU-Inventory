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


_PERIOD_RE = re.compile(
    r"Stock Statement from (\d{2}/\w{3}/\d{4}) to (\d{2}/\w{3}/\d{4})"
)


def parse_meta(raw: pd.DataFrame) -> ReportMeta:
    """Pull the company name, location, and reporting period off the banner rows."""
    company = None
    location = None
    period_start = None
    period_end = None

    for _, row in raw.head(10).iterrows():
        # Banner rows center their text in a merged cell, not always column 0.
        texts = [v.strip() for v in row if isinstance(v, str) and v.strip()]
        if not texts:
            continue
        text = texts[0]
        if company is None:
            company = text
            continue
        match = _PERIOD_RE.search(text)
        if match:
            period_start = pd.to_datetime(match.group(1), format="%d/%b/%Y").date()
            period_end = pd.to_datetime(match.group(2), format="%d/%b/%Y").date()
        elif location is None:
            location = text

    return ReportMeta(company, location, period_start, period_end)


def parse_stock_statement(path: str) -> tuple[pd.DataFrame, ReportMeta]:
    """Parse the raw report into a tidy, one-row-per-SKU DataFrame plus metadata."""
    raw = load_raw(path)
    meta = parse_meta(raw)

    rows: list[dict] = []
    current_brand: str | None = None
    started = False  # becomes True once we've passed the "Opening Stock" header row

    for _, row in raw.iterrows():
        name = row[COL_NAME]
        numeric_vals = row[NUMERIC_COLS]
        has_name = isinstance(name, str) and name.strip() != ""
        all_numeric_blank = numeric_vals.isna().all()

        if not started:
            if str(row[COL_OPENING]).strip() == "Opening Stock":
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
    return df, meta
