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


def _read_excel_safely(path: str, **kwargs) -> pd.DataFrame:
    """pd.read_excel, with a corrupt/wrong-format/partially-saved file turned
    into one plain message instead of whatever raw engine error leaks
    through. The two engines this app uses — xlrd for .xls, openpyxl for
    .xlsx — raise very different, very technical exceptions for "this isn't
    a valid Excel file": CompDocError ("'Book' stream length (1721161
    bytes) > file data size (578328 bytes)") for a truncated .xls,
    BadZipFile for a mangled .xlsx, a bare ValueError ("you must specify an
    engine manually") when the format can't even be detected. None of that
    means anything to someone who uploaded the wrong file, or whose POS
    software's export got cut off mid-save/mid-download — and it's a real
    risk here specifically: this pharmacy's own genuine exports already
    trigger xlrd "OLE2 inconsistency" warnings (tolerated, not fatal) on
    every file, so they're already close to whatever line separates
    "readable with a warning" from "CompDocError."
    """
    try:
        return pd.read_excel(path, **kwargs)
    except Exception as e:
        # Deliberately broad: turning *any* read failure, regardless of
        # which engine or exception type raised it, into one plain message.
        raise ValueError(
            "Couldn't open this file as an Excel spreadsheet "
            f"({e.__class__.__name__}: {e}). It may be the wrong kind of file, corrupted, or "
            "only partially saved or downloaded — try re-exporting it and uploading again."
        ) from e


def load_raw(path: str) -> pd.DataFrame:
    """Read the .xls report as a headerless grid of raw cells."""
    return _read_excel_safely(path, header=None)


def _require_min_columns(raw: pd.DataFrame, min_columns: int, kind: str) -> None:
    """Fail fast, in plain English, when the sheet has fewer columns than
    this format ever reads by fixed position — both parse_stock_statement
    and parse_item_list index columns by trusted fixed position (COL_VALUE,
    IL_COL_LONG_NAME, etc.), so without this check the wrong kind of file
    (or a re-saved copy missing trailing columns) fails with a bare
    ``KeyError: 14`` the moment the row loop reaches a column that isn't
    there — technically accurate, meaningless to whoever's looking at it.
    """
    if raw.shape[1] < min_columns:
        raise ValueError(
            f"This file only has {raw.shape[1]} column(s), but a {kind} export needs at least "
            f"{min_columns}. It may be the wrong kind of file, or a copy that's missing columns — "
            "check it against a known-good export before re-uploading."
        )


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
# Recognising the file only needs the words; the date is only needed to
# compare one item list with another (see sync._record_item_list).
_ITEM_LIST_BANNER_RE = re.compile(r"Item List as on", re.IGNORECASE)


def parse_item_list_meta(raw: pd.DataFrame) -> ItemListMeta:
    """Pull the company name, location, and "as on" date off the banner rows."""
    company, location, match = _scan_banner(raw, _ITEM_LIST_AS_OF_RE)
    as_of = pd.to_datetime(match.group(1), format="%d/%m/%Y").date() if match else None
    return ItemListMeta(company, location, as_of)


def _banner_rows(path: str, sheet) -> pd.DataFrame | None:
    """The first rows of one sheet, or None if that sheet can't be read
    (missing, wrong format, locked) — for sniffing a file's type only."""
    try:
        return pd.read_excel(path, sheet_name=sheet, header=None, nrows=10)
    except Exception:  # noqa: BLE001 — any failure just means "can't tell from this sheet"
        return None


def is_item_list(path: str) -> bool:
    """Whether this file is the POS "item list" export (not a stock
    statement), judged by its banner ("Item List as on dd/mm/yyyy") — so a
    folder scan can recognise one under any filename. Reads only the first
    few rows. Never raises: anything unreadable is simply "not an item list"
    and gets the normal stock-statement handling (and error message).
    """
    return any(
        raw is not None and _scan_banner(raw, _ITEM_LIST_BANNER_RE)[2] is not None
        for raw in (_banner_rows(path, sheet) for sheet in ("Sheet2", 0))
    )


def _item_list_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """The item rows of a raw item list, one per code, with the brand each
    sits under. Column operations, not a row loop (~20,000 rows; see
    _stock_statement_rows).

    Layout, after the header row ("Code" in IL_COL_CODE): a brand header is
    a row with only the code-ish column filled (e.g. "AKSIGEN" on its own),
    followed by that brand's items, each with a code *and* a product name —
    a blank Product cell is what marks a header, not a blank Code cell.

    A row with a product name but no code is skipped: an item can only be
    matched by its code. (It used to get the code "nan", so two of them in
    one list would have blocked the whole upload on a duplicate key.)
    """
    h = _first_header_row(raw, IL_COL_CODE, "Code")
    if h is None:
        return pd.DataFrame(columns=ITEM_LIST_COLUMNS)
    # As with the stock statement's header check: verify a couple more cells
    # are where expected before trusting fixed column positions.
    header = raw.iloc[h]
    if str(header[IL_COL_PRODUCT]).strip() != "Product" or str(header[IL_COL_HSN]).strip() != "HSN":
        raise ValueError(
            "Found the 'Code' header but the other columns don't match the "
            f"expected layout (expected 'Product' in column {IL_COL_PRODUCT + 1} "
            f"and 'HSN' in column {IL_COL_HSN + 1}, got {header[IL_COL_PRODUCT]!r} "
            f"and {header[IL_COL_HSN]!r}). The export template may have changed — "
            "check the file before re-importing."
        )
    body = raw.iloc[h + 1 :]
    code = body[IL_COL_CODE]
    code_text = _stripped_text(code)
    has_code = (code_text.notna() & (code_text != "")) | (code_text.isna() & code.notna())
    names = _stripped_text(body[IL_COL_PRODUCT])
    has_name = names.notna() & (names != "")
    code_str = code.map(lambda v: str(v).strip())
    is_brand = has_code & ~has_name
    brand = code_str.where(is_brand).ffill()
    items = has_code & has_name
    strip_if_text = lambda col: col.map(lambda v: v.strip() if isinstance(v, str) else v)
    rows = body[items]
    return pd.DataFrame(
        {
            "code": code_str[items],
            "brand": brand[items].astype(object).where(brand[items].notna(), None),
            "product_name": names[items],
            "packing": strip_if_text(rows[IL_COL_PACKING]),
            "mrp": rows[IL_COL_MRP],
            "by_rate": rows[IL_COL_BY_RATE],
            "tax_pct": rows[IL_COL_TAX_PCT],
            "hsn": strip_if_text(rows[IL_COL_HSN]),
            "long_name": strip_if_text(rows[IL_COL_LONG_NAME]),
        },
        columns=ITEM_LIST_COLUMNS,
    ).reset_index(drop=True)


def parse_item_list(path: str, sheet_name: str = "Sheet2") -> tuple[pd.DataFrame, ItemListMeta]:
    """Parse the "item list" POS export into a tidy, one-row-per-code DataFrame.

    Structurally similar to the stock statement (brand-header rows interspersed
    with item rows) but a different shape entirely — this maps a stable internal
    `code` to the *current* product name/brand/HSN for every item ever set up in
    the system, which is what lets renamed/re-labeled SKUs in the monthly stock
    statements be flagged rather than silently mismatched.
    """
    raw = _read_excel_safely(path, sheet_name=sheet_name, header=None)
    _require_min_columns(raw, IL_COL_LONG_NAME + 1, "item list")
    meta = parse_item_list_meta(raw)

    df = _item_list_rows(raw)
    numeric_cols = ["mrp", "by_rate", "tax_pct"]
    df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors="coerce")
    return df, meta


def _stripped_text(col: pd.Series) -> pd.Series:
    """Each cell stripped if it's text, NaN otherwise (numbers, blanks)."""
    is_text = col.map(lambda v: isinstance(v, str))
    return col.where(is_text).str.strip()


def _first_header_row(raw: pd.DataFrame, column: int, label: str) -> int | None:
    """Position of the first row whose cell in ``column`` reads ``label``."""
    hits = (raw[column].map(lambda v: str(v).strip()) == label).to_numpy()
    return int(hits.argmax()) if hits.any() else None


def _stock_statement_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """The SKU rows of a raw stock statement, one per SKU, with the brand
    each sits under — numbers still raw (the caller coerces them).

    Works on whole columns rather than row by row: the export is a sheet of
    ~15,000 rows, and a Python loop over it was ~5 seconds per file (most of
    an upload's wait, and ~90 of a fresh 17-file rescan's ~108 seconds).

    Layout, after the header row ("Opening Stock" in COL_OPENING): a
    brand/category name on a row with every number blank, then that brand's
    SKU rows, a "Sub Total" row, a blank separator row, the next brand.
    """
    h = _first_header_row(raw, COL_OPENING, "Opening Stock")
    if h is None:
        return pd.DataFrame(columns=TIDY_COLUMNS)
    # COL_OPENING matching is necessary but not sufficient — the numeric
    # columns are trusted by fixed position from here on (COL_SALES,
    # COL_VALUE, etc.), so if the export template ever adds/reorders a
    # column downstream of "Opening Stock", this one check would still pass
    # while every number read after it is silently wrong (e.g. a "Sales"
    # figure actually read from a "Free" column). Checking a couple more
    # header cells catches that instead of parsing garbage quietly.
    header = raw.iloc[h]
    if str(header[COL_SALES]).strip() != "Sales" or str(header[COL_VALUE]).strip() != "Value":
        raise ValueError(
            "Found the 'Opening Stock' header but the other columns don't match the "
            f"expected layout (expected 'Sales' in column {COL_SALES + 1} and 'Value' in "
            f"column {COL_VALUE + 1}, got {header[COL_SALES]!r} and {header[COL_VALUE]!r}). "
            "The export template may have changed — check the file before re-importing, "
            "since the numbers would otherwise be read from the wrong columns silently."
        )
    body = raw.iloc[h + 1 :]
    names = _stripped_text(body[COL_NAME])
    keep = names.notna() & (names != "") & ~names.isin(["Sub Total", "Grand Total"])
    body, names = body[keep], names[keep]
    # A row with a name and every number blank is a brand/category header;
    # each SKU row belongs to the last one above it.
    is_brand = body[NUMERIC_COLS].isna().all(axis=1)
    brand = names.where(is_brand).ffill()
    skus = body[~is_brand]
    out = pd.DataFrame(
        {
            "brand": brand[~is_brand].astype(object).where(brand[~is_brand].notna(), None),
            "sku": names[~is_brand],
            "opening_stock": skus[COL_OPENING],
            "purchase": skus[COL_PURCHASE],
            "purchase_free": skus[COL_PURCHASE_FREE],
            "other_receipt": skus[COL_OTHER_RECEIPT],
            "sales": skus[COL_SALES],
            "sales_free": skus[COL_SALES_FREE],
            "other_issue": skus[COL_OTHER_ISSUE],
            "closing_stock": skus[COL_CLOSING_STOCK],
            "value": skus[COL_VALUE],
        },
        columns=TIDY_COLUMNS,
    )
    return out.reset_index(drop=True)


def parse_stock_statement(path: str) -> tuple[pd.DataFrame, ReportMeta]:
    """Parse the raw report into a tidy, one-row-per-SKU DataFrame plus metadata."""
    raw = load_raw(path)
    _require_min_columns(raw, COL_VALUE + 1, "stock statement")
    meta = parse_meta(raw)

    df = _stock_statement_rows(raw)
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
