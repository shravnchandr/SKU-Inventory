"""SQLite storage for imported stock statements.

Each import (one .xls upload) becomes a "report": a period (start/end date)
plus one row per SKU active in that period. Reports accumulate over time —
one per month, going forward — so the app can compute a current snapshot
(latest report) and trailing sales trends (recent reports) without re-parsing
old .xls files.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TypedDict

import pandas as pd

from . import analysis
from .analysis import Settings
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

-- Speeds up the overlap query (find_overlapping_reports) and list_reports'
-- ORDER BY period_end as this table grows from tens of rows (monthly
-- cadence) to hundreds/thousands (daily). A plain index, unlike the unique
-- one on stock_entries, can't fail against existing data, so it doesn't
-- need the _migrate()-time dance — safe straight in SCHEMA.
CREATE INDEX IF NOT EXISTS idx_reports_period_end ON reports(period_end);

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
    value_tier_a_pct INTEGER NOT NULL DEFAULT 70,
    value_tier_b_pct INTEGER NOT NULL DEFAULT 90,
    -- Monotonic counter, not just a timestamp: datetime('now') is only
    -- second-resolution, so two saves within the same second wouldn't
    -- otherwise be distinguishable — which matters because this feeds the
    -- webapp's cache-invalidation fingerprint (see webapp.get_current_data).
    version INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The POS system's master item list: a stable internal `code` per item,
-- mapped to its *current* product name/brand/HSN. Names in the monthly
-- stock statements drift over time (renames, re-labeling); this table has
-- no shared key with those statements, so it can't auto-fix a rename, but
-- it can flag a currently-stocked SKU name that no longer matches anything
-- here at all. Whole-table replace on every import (see import_item_catalog)
-- since there's only ever one "current" catalog, not one per period.
--
-- packing/mrp/by_rate/tax_pct/hsn are captured field-for-field from the POS
-- export but currently write-only — no query reads them back out yet. Kept
-- deliberately, not dropped: retained source data for a plausible
-- near-future feature (e.g. margin/price/pack-size checks on the SKU detail
-- page) rather than accidental cruft.
CREATE TABLE IF NOT EXISTS item_catalog (
    code TEXT PRIMARY KEY,
    brand TEXT,
    product_name TEXT NOT NULL,
    packing TEXT,
    mrp REAL,
    by_rate REAL,
    tax_pct REAL,
    hsn TEXT,
    long_name TEXT,
    imported_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_item_catalog_product ON item_catalog(product_name);

-- A code-anchored rename: item_catalog has no history of its own (it's a
-- full replace on every import), so whenever a re-import finds an existing
-- code now carrying a different name, that transition is logged here before
-- the old value is lost. Same code = definitely the same item, so this is
-- used to pair off a "vanished" and "new" SKU name in find_sku_churn without
-- guessing from text similarity. Append-only — a rename detected months ago
-- should still resolve churn today.
CREATE TABLE IF NOT EXISTS item_name_changes (
    code TEXT NOT NULL,
    old_name TEXT NOT NULL,
    new_name TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_item_name_changes_old ON item_name_changes(old_name);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to tables that already existed before they gained new
    columns — "CREATE TABLE IF NOT EXISTS" is a no-op against a database
    that already has the table, so a new NOT NULL column with a DEFAULT
    needs an explicit ALTER TABLE for anyone upgrading from an older schema.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(settings)")}
    if "value_tier_a_pct" not in existing:
        conn.execute("ALTER TABLE settings ADD COLUMN value_tier_a_pct INTEGER NOT NULL DEFAULT 70")
    if "value_tier_b_pct" not in existing:
        conn.execute("ALTER TABLE settings ADD COLUMN value_tier_b_pct INTEGER NOT NULL DEFAULT 90")

    _dedupe_stock_entries(conn)
    # A backstop, not the primary defense — parser.py now aggregates
    # duplicate SKU rows within one report before they're ever inserted, so
    # this should never actually reject anything going forward. But adding
    # a plain CREATE UNIQUE INDEX straight into SCHEMA would run before this
    # function on every connect() and fail outright on any database that
    # already had duplicates from before that fix existed — hence the
    # dedupe pass immediately above, and creating the index here instead.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entries_report_sku ON stock_entries(report_id, sku)"
    )


_STOCK_ENTRY_NUMERIC_COLS = [
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


def _dedupe_stock_entries(conn: sqlite3.Connection) -> None:
    """Collapse any pre-existing (report_id, sku) duplicates by summing
    their numeric columns — the same rule parser.py now applies before
    import, applied here once for any database that predates that fix, so
    the UNIQUE index below can actually be created.
    """
    has_dupes = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM stock_entries GROUP BY report_id, sku HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    if not has_dupes:
        return
    df = pd.read_sql("SELECT * FROM stock_entries", conn)
    deduped = df.groupby(["report_id", "sku"], as_index=False).agg(
        {"brand": "first", **{col: "sum" for col in _STOCK_ENTRY_NUMERIC_COLS}}
    )
    conn.execute("DELETE FROM stock_entries")
    deduped[["report_id", "brand", "sku"] + _STOCK_ENTRY_NUMERIC_COLS].to_sql(
        "stock_entries", conn, if_exists="append", index=False
    )


@contextmanager
def connect(db_path: str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
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
    superseded_report_ids: list[int] = field(default_factory=list)


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


def find_reused_filename_conflict(
    conn: sqlite3.Connection, known: sqlite3.Row | None, meta: ReportMeta
) -> sqlite3.Row | None:
    """If `known` (a watched_files row for this filename) points at a report
    whose period doesn't match `meta`'s, return that stale report — the
    filename now represents a different period than it used to, without the
    old report being removed first. Returns None if there's no such
    conflict. Shared by sync.py's folder scan and webapp.py's direct
    upload — both need this same check, they just handle a real conflict
    differently (log-and-skip vs. an immediate HTTP error).
    """
    if known is None or known["report_id"] is None:
        return None
    old_report = get_report(conn, known["report_id"])
    if old_report is None:
        return None
    if (
        old_report["period_start"] != meta.period_start.isoformat()
        or old_report["period_end"] != meta.period_end.isoformat()
    ):
        return old_report
    return None


def delete_report(conn: sqlite3.Connection, report_id: int) -> bool:
    """Delete a report and its stock entries. Also forgets any watched_files
    fingerprint pointing at it, so a refresh will re-import the source file
    (rather than skip it as "unchanged") if it's still sitting in uploads/.
    """
    conn.execute("DELETE FROM watched_files WHERE report_id = ?", (report_id,))
    cur = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    return cur.rowcount > 0


def find_overlapping_reports(conn: sqlite3.Connection, meta: ReportMeta) -> list[int]:
    """Reports whose date range overlaps the new one — a genuinely different
    scenario from period_exists's exact match (handled separately via
    replace), and excluded from this query's results so the two mechanisms
    never double-handle the same report.

    Every report this returns gets deleted and replaced by the new upload,
    however the overlap is shaped — no partial-overlap case is rejected.
    Overlaps aren't expected in normal use (reports are meant to cover
    disjoint periods), but when one does happen the newest upload is always
    treated as correct: simpler for daily-cadence uploads than asking
    someone to manually reconcile which side of a disputed date range is
    right, at the cost of trusting whichever file was imported most
    recently to actually be the accurate one.
    """
    rows = conn.execute(
        "SELECT id FROM reports "
        "WHERE period_start <= ? AND period_end >= ? AND NOT (period_start = ? AND period_end = ?)",
        (
            meta.period_end.isoformat(),
            meta.period_start.isoformat(),
            meta.period_start.isoformat(),
            meta.period_end.isoformat(),
        ),
    ).fetchall()
    return [r[0] for r in rows]


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

    overlapping_ids = find_overlapping_reports(conn, meta)
    for report_id in overlapping_ids:
        # Not a raw DELETE — this may be a different source file than the
        # overlapping report's own, so its watched_files fingerprint (if any)
        # needs clearing too, or a future rescan would find a report_id that
        # no longer exists.
        delete_report(conn, report_id)
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
        "report_id",
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
    rows[cols].to_sql("stock_entries", conn, if_exists="append", index=False)

    return ImportResult(
        report_id, meta.period_start, meta.period_end, len(df), replaced, overlapping_ids
    )


def list_reports(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT id, company, location, period_start, period_end, period_days, "
        "source_filename, imported_at, "
        "(SELECT COUNT(*) FROM stock_entries e WHERE e.report_id = r.id) AS sku_count "
        "FROM reports r ORDER BY period_end",
        conn,
        parse_dates=["period_start", "period_end"],
    )


_ENTRIES_SELECT = (
    "SELECT e.*, r.period_start, r.period_end, r.period_days, r.company, r.location "
    "FROM stock_entries e JOIN reports r ON r.id = e.report_id"
)


def load_all_entries(conn: sqlite3.Connection) -> pd.DataFrame:
    """All stock entries across every imported report, joined with report period info.

    Deliberately unordered: SQLite has to build and sort a full temp result set
    to satisfy an ORDER BY over millions of rows, which gets expensive as years
    of history pile up — and every caller (analysis.py) already filters/groups
    or does its own explicit .sort_values() on whatever subset it cares about,
    so nothing here actually depends on row order coming back from SQL.

    Unbounded — scans every row of every report ever imported. Fine at
    monthly cadence; under daily-cadence uploads this grows ~30x faster per
    unit of calendar time, so webapp.py's cache prefers extending a
    previously-cached frame with just the new reports (see
    load_entries_for_reports) over calling this on every cache miss, falling
    back to a full call here only when something was actually removed.
    """
    return pd.read_sql(_ENTRIES_SELECT, conn, parse_dates=["period_start", "period_end"])


def get_report_ids(conn: sqlite3.Connection) -> set[int]:
    """Every report id currently in the database — cheap (reports stays a
    small table even under daily cadence), used by webapp.py's cache to
    decide whether it can extend what it already has or needs a full reload.
    """
    return {r[0] for r in conn.execute("SELECT id FROM reports").fetchall()}


def load_entries_for_reports(conn: sqlite3.Connection, report_ids) -> pd.DataFrame:
    """Same shape as load_all_entries, scoped to just the given report ids —
    for extending an already-cached frame with only the reports that are
    new since it was built, instead of re-fetching all of history.
    """
    ids = list(report_ids)
    if not ids:
        return pd.read_sql(f"{_ENTRIES_SELECT} WHERE 0", conn, parse_dates=["period_start", "period_end"])
    placeholders = ",".join("?" * len(ids))
    return pd.read_sql(
        f"{_ENTRIES_SELECT} WHERE e.report_id IN ({placeholders})",
        conn,
        params=ids,
        parse_dates=["period_start", "period_end"],
    )


def has_data(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT COUNT(*) FROM reports").fetchone()
    return bool(row and row[0] > 0)


def get_watched_file(conn: sqlite3.Connection, filename: str) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM watched_files WHERE filename = ?", (filename,)).fetchone()
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
    "value_tier_a_pct": analysis.VALUE_TIER_A_PCT,
    "value_tier_b_pct": analysis.VALUE_TIER_B_PCT,
}


def get_settings(conn: sqlite3.Connection) -> Settings:
    """Persisted status thresholds, or the built-in defaults if never saved."""
    row = conn.execute(
        "SELECT low_stock_days, overstock_days, trailing_days_target, dead_stock_days, "
        "value_tier_a_pct, value_tier_b_pct, version, updated_at "
        "FROM settings WHERE id = 1"
    ).fetchone()
    if row is None:
        return {**DEFAULT_SETTINGS, "version": 0, "updated_at": None}
    keys = [
        "low_stock_days",
        "overstock_days",
        "trailing_days_target",
        "dead_stock_days",
        "value_tier_a_pct",
        "value_tier_b_pct",
        "version",
        "updated_at",
    ]
    return dict(zip(keys, row))


def upsert_settings(
    conn: sqlite3.Connection,
    low_stock_days: int,
    overstock_days: int,
    trailing_days_target: int,
    dead_stock_days: int,
    value_tier_a_pct: int,
    value_tier_b_pct: int,
) -> Settings:
    conn.execute(
        "INSERT INTO settings (id, low_stock_days, overstock_days, trailing_days_target, dead_stock_days, "
        "value_tier_a_pct, value_tier_b_pct, version, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?, 1, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "low_stock_days=excluded.low_stock_days, overstock_days=excluded.overstock_days, "
        "trailing_days_target=excluded.trailing_days_target, dead_stock_days=excluded.dead_stock_days, "
        "value_tier_a_pct=excluded.value_tier_a_pct, value_tier_b_pct=excluded.value_tier_b_pct, "
        "version=settings.version + 1, updated_at=excluded.updated_at",
        (
            low_stock_days,
            overstock_days,
            trailing_days_target,
            dead_stock_days,
            value_tier_a_pct,
            value_tier_b_pct,
        ),
    )
    return get_settings(conn)


def import_item_catalog(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """Replace the whole item_catalog with `df` (one row per code). There's
    only ever one "current" catalog — unlike reports, which each represent a
    distinct period and accumulate — so every import is a full replace, not
    an accumulation.

    Before replacing, diff the incoming rows against what's stored for each
    code: a code that already existed but now carries a different
    product_name/long_name just got renamed in the POS system, and that
    transition is logged to item_name_changes — otherwise the old name is
    gone the moment this DELETE runs, with nothing left to pair it against.
    """
    previous = pd.read_sql("SELECT code, product_name, long_name FROM item_catalog", conn)
    prev_names = {
        row.code: (row.product_name, row.long_name) for row in previous.itertuples(index=False)
    }

    count = _replace_item_catalog_table(conn, df)

    changes = []
    for row in df.itertuples(index=False):
        prev = prev_names.get(row.code)
        if prev is None:
            continue
        old_product, old_long = prev
        if old_product and row.product_name and old_product != row.product_name:
            changes.append((row.code, old_product, row.product_name))
        if old_long and row.long_name and old_long != row.long_name and old_long != old_product:
            changes.append((row.code, old_long, row.long_name))
    if changes:
        conn.executemany(
            "INSERT INTO item_name_changes (code, old_name, new_name) VALUES (?, ?, ?)",
            changes,
        )
    return count


ITEM_CATALOG_COLUMNS = [
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


def _replace_item_catalog_table(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    conn.execute("DELETE FROM item_catalog")
    df[ITEM_CATALOG_COLUMNS].to_sql("item_catalog", conn, if_exists="append", index=False)
    return len(df)


def get_item_catalog_df(conn: sqlite3.Connection) -> pd.DataFrame:
    """The full current catalog, in the shape import_item_catalog/
    rollback_item_catalog expect — used to snapshot the catalog before an
    upload, so a failure partway through can be rolled back to it.
    """
    return pd.read_sql(f"SELECT {', '.join(ITEM_CATALOG_COLUMNS)} FROM item_catalog", conn)


def get_item_name_changes_watermark(conn: sqlite3.Connection) -> int:
    """The highest item_name_changes rowid that exists right now — snapshot
    this immediately before an import_item_catalog call (alongside
    get_item_catalog_df) so a rollback can identify exactly the alias rows
    *that import* added, without touching any real rename history a prior,
    successful import already committed.
    """
    row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM item_name_changes").fetchone()
    return row[0]


def rollback_item_catalog(
    conn: sqlite3.Connection, catalog_snapshot: pd.DataFrame, item_name_changes_watermark: int
) -> int:
    """Undo an import_item_catalog call that committed to the DB but whose
    file write afterward failed: restores the catalog table to its
    pre-import snapshot, and deletes any item_name_changes rows that import
    added (rowid > item_name_changes_watermark). Without the second part, a
    rename recorded by the failed import would linger in item_name_changes
    forever — the code/name transition never actually took effect (the
    catalog itself was rolled back), so find_sku_churn pairing a future
    vanished/new SKU off of it would be pairing off a rename that never
    happened.
    """
    conn.execute("DELETE FROM item_name_changes WHERE rowid > ?", (item_name_changes_watermark,))
    return _replace_item_catalog_table(conn, catalog_snapshot)


def get_name_change_map(conn: sqlite3.Connection) -> dict[str, str]:
    """old_name -> latest known new_name, from every code-anchored rename an
    item_catalog re-import has ever detected. Used by find_sku_churn to pair
    a vanished/new SKU name without guessing from text similarity.
    """
    rows = conn.execute(
        "SELECT old_name, new_name FROM item_name_changes ORDER BY rowid ASC"
    ).fetchall()
    mapping: dict[str, str] = {}
    for old_name, new_name in rows:
        mapping[old_name] = new_name
    return mapping


class CatalogMeta(TypedDict):
    imported_at: str
    item_count: int


def get_item_catalog_meta(conn: sqlite3.Connection) -> CatalogMeta | None:
    """When the catalog was last imported and how many items it has, or None
    if nothing has been imported yet.
    """
    row = conn.execute("SELECT MAX(imported_at), COUNT(*) FROM item_catalog").fetchone()
    if row is None or row[1] == 0:
        return None
    return {"imported_at": row[0], "item_count": row[1]}


def list_item_catalog_names(conn: sqlite3.Connection) -> set[str]:
    """Every distinct product_name and long_name in the catalog, for matching
    against currently-stocked SKU names. Just strings — cheap enough not to
    round-trip the whole table for this check.
    """
    rows = conn.execute(
        "SELECT product_name FROM item_catalog UNION SELECT long_name FROM item_catalog"
    ).fetchall()
    return {r[0] for r in rows if r[0]}
