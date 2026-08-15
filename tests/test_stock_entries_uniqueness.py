"""db.py's stock_entries(report_id, sku) UNIQUE index — a backstop behind
parser.py's duplicate-row aggregation (see
test_stock_statement_duplicate_skus.py), not the primary defense. Covers:
the one-time migration that dedupes a pre-existing database before the
index can even be created, and that the index actually rejects a duplicate
insert going forward.
"""
import sqlite3

import pytest

from src.gowri_proj import db


def test_migration_dedupes_a_pre_existing_database_before_creating_the_index(tmp_path):
    path = tmp_path / "legacy.db"
    # Simulate a database from before this fix existed: two rows for the
    # same (report_id, sku), inserted directly (bypassing parser.py's
    # dedup, exactly as an older version of this app would have allowed).
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT, location TEXT,
            period_start TEXT NOT NULL, period_end TEXT NOT NULL, period_days INTEGER NOT NULL,
            source_filename TEXT, imported_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (period_start, period_end)
        );
        CREATE TABLE stock_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER NOT NULL, brand TEXT, sku TEXT NOT NULL,
            opening_stock REAL NOT NULL, purchase REAL NOT NULL, purchase_free REAL NOT NULL,
            other_receipt REAL NOT NULL, sales REAL NOT NULL, sales_free REAL NOT NULL,
            other_issue REAL NOT NULL, closing_stock REAL NOT NULL, value REAL NOT NULL
        );
        """
    )
    raw.execute(
        "INSERT INTO reports (id, company, location, period_start, period_end, period_days) "
        "VALUES (1, 'X', 'Y', '2026-06-01', '2026-06-30', 30)"
    )
    raw.executemany(
        "INSERT INTO stock_entries (report_id, brand, sku, opening_stock, purchase, purchase_free, "
        "other_receipt, sales, sales_free, other_issue, closing_stock, value) VALUES (1, ?, ?, ?, ?, 0, 0, ?, 0, 0, ?, ?)",
        [
            ("BRAND A", "DUPLICATED SKU", 10, 5, 3, 12, 120.0),
            ("BRAND A", "DUPLICATED SKU", 4, 1, 2, 3, 30.0),
            ("BRAND A", "STABLE SKU", 20, 0, 10, 10, 200.0),
        ],
    )
    raw.commit()
    raw.close()

    # Opening through db.connect() runs SCHEMA (adds any missing pieces)
    # then _migrate() — which must dedupe *before* trying to create the
    # UNIQUE index, or the index creation itself would fail against this
    # exact database.
    with db.connect(str(path)) as conn:
        rows = conn.execute(
            "SELECT sku, opening_stock, purchase, sales, closing_stock, value FROM stock_entries ORDER BY sku"
        ).fetchall()

    by_sku = {r[0]: r[1:] for r in rows}
    assert set(by_sku) == {"DUPLICATED SKU", "STABLE SKU"}
    assert by_sku["DUPLICATED SKU"] == (14, 6, 5, 15, 150.0)
    assert by_sku["STABLE SKU"] == (20, 0, 10, 10, 200.0)


def test_unique_index_rejects_a_duplicate_sku_insert_after_migration(tmp_path):
    path = tmp_path / "test.db"
    with db.connect(str(path)) as conn:
        conn.execute(
            "INSERT INTO reports (company, location, period_start, period_end, period_days) "
            "VALUES ('X', 'Y', '2026-06-01', '2026-06-30', 30)"
        )
        report_id = conn.execute("SELECT id FROM reports").fetchone()[0]
        conn.execute(
            "INSERT INTO stock_entries (report_id, brand, sku, opening_stock, purchase, purchase_free, "
            "other_receipt, sales, sales_free, other_issue, closing_stock, value) "
            "VALUES (?, 'BRAND A', 'SKU ONE', 1, 0, 0, 0, 0, 0, 0, 1, 10)",
            (report_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO stock_entries (report_id, brand, sku, opening_stock, purchase, purchase_free, "
                "other_receipt, sales, sales_free, other_issue, closing_stock, value) "
                "VALUES (?, 'BRAND A', 'SKU ONE', 2, 0, 0, 0, 0, 0, 0, 2, 20)",
                (report_id,),
            )
