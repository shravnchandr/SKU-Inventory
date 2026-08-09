"""SQLite storage for imported stock statements.

Each import (one .xls upload) becomes a "report": a period (start/end date)
plus one row per SKU active in that period. Reports accumulate over time —
one per month, going forward — so the app can compute a current snapshot
(latest report) and trailing sales trends (recent reports) without re-parsing
old .xls files.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from . import analysis
from .parser import ReportMeta

DEFAULT_DB_PATH = "db/inventory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT,
    location TEXT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_days INTEGER NOT NULL,
    source_filename TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (period_start, period_end)
);

CREATE TABLE IF NOT EXISTS stock_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    brand TEXT,
    sku TEXT NOT NULL,
    opening_stock REAL NOT NULL,
    purchase REAL NOT NULL,
    purchase_free REAL NOT NULL,
    other_receipt REAL NOT NULL,
    sales REAL NOT NULL,
    sales_free REAL NOT NULL,
    other_issue REAL NOT NULL,
    closing_stock REAL NOT NULL,
    value REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_report ON stock_entries(report_id);
CREATE INDEX IF NOT EXISTS idx_entries_sku ON stock_entries(brand, sku);

-- Tracks every file a folder sync has looked at, so an unchanged file can be
-- skipped without re-parsing it on the next refresh.
CREATE TABLE IF NOT EXISTS watched_files (
    filename TEXT PRIMARY KEY,
    filesize INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    report_id INTEGER REFERENCES reports(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    detail TEXT,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Single-row table (id is always 1) holding the user-configurable status
-- thresholds. Lazily created — no row exists until the first save from the
-- Settings page, at which point get_settings() falls back to the analysis
-- module's hardcoded defaults.
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    low_stock_days INTEGER NOT NULL,
    overstock_days INTEGER NOT NULL,
    trailing_days_target INTEGER NOT NULL,
    dead_stock_days INTEGER NOT NULL,
    -- Monotonic counter, not just a timestamp: datetime('now') is only
    -- second-resolution, so two saves within the same second wouldn't
    -- otherwise be distinguishable — which matters because this feeds the
    -- webapp's cache-invalidation fingerprint (see webapp.get_current_data).
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@contextmanager
def connect(db_path: str = DEFAULT_DB_PATH):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class ImportResult:
    report_id: int
    period_start: date
    period_end: date
    sku_count: int
    replaced: bool


def period_exists(conn: sqlite3.Connection, meta: ReportMeta) -> int | None:
    row = conn.execute(
        "SELECT id FROM reports WHERE period_start = ? AND period_end = ?",
        (meta.period_start.isoformat(), meta.period_end.isoformat()),
    ).fetchone()
    return row[0] if row else None


def get_report(conn: sqlite3.Connection, report_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, company, location, period_start, period_end, period_days, source_filename, "
        "(SELECT COUNT(*) FROM stock_entries e WHERE e.report_id = r.id) AS sku_count "
        "FROM reports r WHERE id = ?",
        (report_id,),
    ).fetchone()
    conn.row_factory = None
    return row


def delete_report(conn: sqlite3.Connection, report_id: int) -> bool:
    """Delete a report and its stock entries. Also forgets any watched_files
    fingerprint pointing at it, so a refresh will re-import the source file
    (rather than skip it as "unchanged") if it's still sitting in uploads/.
    """
    conn.execute("DELETE FROM watched_files WHERE report_id = ?", (report_id,))
    cur = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    return cur.rowcount > 0


def import_report(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    meta: ReportMeta,
    source_filename: str,
    replace: bool = False,
) -> ImportResult:
    if not meta.period_start or not meta.period_end:
        raise ValueError(
            "Could not read a reporting period from this file — expected a "
            "'Stock Statement from DD/Mon/YYYY to DD/Mon/YYYY' banner row."
        )
    if df.empty:
        raise ValueError(
            "No SKU rows could be read from this file — it may not be a stock statement export."
        )

    existing_id = period_exists(conn, meta)
    replaced = False
    if existing_id is not None:
        if not replace:
            raise ValueError(
                f"A report for {meta.period_start} to {meta.period_end} is already "
                f"imported (id={existing_id}). Pass --replace to overwrite it."
            )
        conn.execute("DELETE FROM reports WHERE id = ?", (existing_id,))
        replaced = True

    cur = conn.execute(
        "INSERT INTO reports (company, location, period_start, period_end, period_days, source_filename) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            meta.company,
            meta.location,
            meta.period_start.isoformat(),
            meta.period_end.isoformat(),
            meta.period_days,
            source_filename,
        ),
    )
    report_id = cur.lastrowid

    rows = df.copy()
    rows.insert(0, "report_id", report_id)
    cols = [
        "report_id", "brand", "sku", "opening_stock", "purchase", "purchase_free",
        "other_receipt", "sales", "sales_free", "other_issue", "closing_stock", "value",
    ]
    rows[cols].to_sql("stock_entries", conn, if_exists="append", index=False)

    return ImportResult(report_id, meta.period_start, meta.period_end, len(df), replaced)


def list_reports(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, company, location, period_start, period_end, period_days, "
        "source_filename, imported_at, "
        "(SELECT COUNT(*) FROM stock_entries e WHERE e.report_id = r.id) AS sku_count "
        "FROM reports r ORDER BY period_end",
        conn,
        parse_dates=["period_start", "period_end"],
    )


def load_all_entries(conn: sqlite3.Connection) -> pd.DataFrame:
    """All stock entries across every imported report, joined with report period info.

    Deliberately unordered: SQLite has to build and sort a full temp result set
    to satisfy an ORDER BY over millions of rows, which gets expensive as years
    of history pile up — and every caller (analysis.py) already filters/groups
    or does its own explicit .sort_values() on whatever subset it cares about,
    so nothing here actually depends on row order coming back from SQL.
    """
    return pd.read_sql(
        "SELECT e.*, r.period_start, r.period_end, r.period_days, r.company, r.location "
        "FROM stock_entries e JOIN reports r ON r.id = e.report_id",
        conn,
        parse_dates=["period_start", "period_end"],
    )


def has_data(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM reports").fetchone()
    return bool(row and row[0] > 0)


def get_watched_file(conn: sqlite3.Connection, filename: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM watched_files WHERE filename = ?", (filename,)
    ).fetchone()
    conn.row_factory = None
    return row


def upsert_watched_file(
    conn: sqlite3.Connection,
    filename: str,
    filesize: int,
    mtime: int,
    report_id: int | None,
    status: str,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO watched_files (filename, filesize, mtime, report_id, status, detail, checked_at) "
        "VALUES (?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(filename) DO UPDATE SET "
        "filesize=excluded.filesize, mtime=excluded.mtime, report_id=excluded.report_id, "
        "status=excluded.status, detail=excluded.detail, checked_at=excluded.checked_at",
        (filename, filesize, mtime, report_id, status, detail),
    )


def list_watched_files(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql("SELECT * FROM watched_files ORDER BY filename", conn)


def list_watched_files_problems(conn: sqlite3.Connection, limit: int = 20) -> pd.DataFrame:
    """Files from the last scan(s) that were rejected rather than imported."""
    return pd.read_sql(
        "SELECT * FROM watched_files WHERE status != 'imported' ORDER BY checked_at DESC LIMIT ?",
        conn,
        params=(limit,),
    )


def last_refresh_time(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(checked_at) FROM watched_files").fetchone()
    return row[0] if row else None


DEFAULT_SETTINGS = {
    "low_stock_days": analysis.LOW_STOCK_DAYS,
    "overstock_days": analysis.OVERSTOCK_DAYS,
    "trailing_days_target": analysis.TRAILING_DAYS_TARGET,
    "dead_stock_days": analysis.DEAD_STOCK_DAYS,
}


def get_settings(conn: sqlite3.Connection) -> dict:
    """Persisted status thresholds, or the built-in defaults if never saved."""
    row = conn.execute(
        "SELECT low_stock_days, overstock_days, trailing_days_target, dead_stock_days, version, updated_at "
        "FROM settings WHERE id = 1"
    ).fetchone()
    if row is None:
        return {**DEFAULT_SETTINGS, "version": 0, "updated_at": None}
    keys = ["low_stock_days", "overstock_days", "trailing_days_target", "dead_stock_days", "version", "updated_at"]
    return dict(zip(keys, row))


def upsert_settings(
    conn: sqlite3.Connection,
    low_stock_days: int,
    overstock_days: int,
    trailing_days_target: int,
    dead_stock_days: int,
) -> dict:
    conn.execute(
        "INSERT INTO settings (id, low_stock_days, overstock_days, trailing_days_target, dead_stock_days, version, updated_at) "
        "VALUES (1, ?, ?, ?, ?, 1, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "low_stock_days=excluded.low_stock_days, overstock_days=excluded.overstock_days, "
        "trailing_days_target=excluded.trailing_days_target, dead_stock_days=excluded.dead_stock_days, "
        "version=settings.version + 1, updated_at=excluded.updated_at",
        (low_stock_days, overstock_days, trailing_days_target, dead_stock_days),
    )
    return get_settings(conn)
