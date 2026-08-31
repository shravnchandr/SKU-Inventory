"""Endpoint tests for the "download the full list" CSV exports on the
Reports page: /api/unmatched-skus/export and /api/sku-churn/export.

Both exist because /api/import-health deliberately caps these same two
lists for the page itself (see its own comments) — the export endpoints
re-run the same analysis uncapped so a download always has every row,
not just the top-by-value subset the page shows.
"""

from datetime import date

import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.webapp import create_app


def _meta(start, end):
    return ReportMeta(
        company="TEST PHARMACY", location="TEST CITY",
        period_start=date.fromisoformat(start), period_end=date.fromisoformat(end),
    )


def _df(*skus, brand="BRAND"):
    rows = [
        {
            "brand": brand, "sku": sku, "opening_stock": 1.0, "purchase": 0.0,
            "purchase_free": 0.0, "other_receipt": 0.0, "sales": 1.0, "sales_free": 0.0,
            "other_issue": 0.0, "closing_stock": 1.0, "value": 10.0,
        }
        for sku in skus
    ]
    return pd.DataFrame(rows, columns=TIDY_COLUMNS)


def _catalog_df(codes_and_names):
    rows = [
        {
            "code": code, "brand": "BRAND", "product_name": name, "packing": "1S",
            "mrp": 1.0, "by_rate": 1.0, "tax_pct": 12.0, "hsn": "3004", "long_name": None,
        }
        for code, name in codes_and_names
    ]
    cols = ["code", "brand", "product_name", "packing", "mrp", "by_rate", "tax_pct", "hsn", "long_name"]
    return pd.DataFrame(rows)[cols]


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    with app.test_client() as c:
        yield c, app


def test_unmatched_skus_export_includes_every_row_not_just_the_capped_display_set(client):
    c, app = client
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(conn, _df("A", "B"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")
        # Catalog only vouches for "A" — "B" is unmatched.
        db.import_item_catalog(conn, _catalog_df([("A", "A")]))

    resp = c.get("/api/unmatched-skus/export")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert "attachment" in resp.headers["Content-Disposition"]
    body = resp.get_data(as_text=True)
    assert "B" in body
    assert "\nA," not in body  # A is matched, shouldn't appear as unmatched


def test_unmatched_skus_export_is_empty_with_no_catalog(client):
    c, app = client
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")

    resp = c.get("/api/unmatched-skus/export")
    assert resp.status_code == 200
    lines = resp.get_data(as_text=True).splitlines()
    assert len(lines) == 1  # header row only


def test_sku_churn_export_lists_both_new_and_vanished_skus(client):
    c, app = client
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(conn, _df("A", "B"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")
        db.import_report(conn, _df("A", "C"), _meta("2026-07-01", "2026-07-31"), "july.xlsx")

    resp = c.get("/api/sku-churn/export")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "New,BRAND,C," in body
    assert "Vanished,BRAND,B," in body
    assert ",A," not in body  # A is unchanged, not churn


def test_sku_churn_export_is_empty_with_fewer_than_two_reports(client):
    c, app = client
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-30"), "june.xlsx")

    resp = c.get("/api/sku-churn/export")
    assert resp.status_code == 200
    lines = resp.get_data(as_text=True).splitlines()
    assert len(lines) == 1  # header row only
