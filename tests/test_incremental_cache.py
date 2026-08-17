"""webapp.get_current_data's incremental cache: under daily-cadence uploads,
db.load_all_entries (a full unbounded history scan) previously ran on every
cache miss, including a settings-only save. This covers the three paths the
reshaped cache is supposed to take:

1. Pure addition (the normal daily-import case) — extends the cached frame
   with just the new report's rows via load_entries_for_reports, without
   calling load_all_entries again.
2. A report removed/superseded — falls back to a full load_all_entries
   (extending isn't safe once something might be missing from the cache).
3. A settings-only change — reuses the already-cached all_entries object
   untouched (no reload at all) and only recomputes the summary.
"""

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.webapp import create_app


def _meta(start, end):
    return ReportMeta(
        company="TEST PHARMACY",
        location="TEST CITY",
        period_start=date.fromisoformat(start),
        period_end=date.fromisoformat(end),
    )


def _df(*skus):
    rows = [
        {
            "brand": "BRAND",
            "sku": sku,
            "opening_stock": 1.0,
            "purchase": 0.0,
            "purchase_free": 0.0,
            "other_receipt": 0.0,
            "sales": 1.0,
            "sales_free": 0.0,
            "other_issue": 0.0,
            "closing_stock": 1.0,
            "value": 10.0,
        }
        for sku in skus
    ]
    return pd.DataFrame(rows, columns=TIDY_COLUMNS)


@pytest.fixture
def app_and_client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    with app.test_client() as c:
        yield app, c


def _hit_import_health(client, app):
    resp = client.get("/api/import-health", headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]})
    assert resp.status_code == 200
    return resp


def test_pure_addition_extends_the_cache_without_a_full_reload(app_and_client, tmp_path):
    app, client = app_and_client
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-01"), "day1.xlsx")

    _hit_import_health(client, app)
    cache = app.config["_SUMMARY_CACHE"]
    assert len(cache["all_entries"]) == 1
    first_frame_id = id(cache["all_entries"])

    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("B"), _meta("2026-06-02", "2026-06-02"), "day2.xlsx")

    with (
        patch.object(db, "load_all_entries", wraps=db.load_all_entries) as spy_full,
        patch.object(db, "load_entries_for_reports", wraps=db.load_entries_for_reports) as spy_incremental,
    ):
        _hit_import_health(client, app)

    spy_full.assert_not_called()
    spy_incremental.assert_called_once()
    cache = app.config["_SUMMARY_CACHE"]
    assert len(cache["all_entries"]) == 2
    assert set(cache["all_entries"]["sku"]) == {"A", "B"}
    # A genuinely different frame (extended), not the same object mutated in place.
    assert id(cache["all_entries"]) != first_frame_id


def test_a_removed_report_falls_back_to_a_full_reload(app_and_client, tmp_path):
    app, client = app_and_client
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-01"), "day1.xlsx")
        keep = db.import_report(conn, _df("B"), _meta("2026-06-02", "2026-06-02"), "day2.xlsx")

    _hit_import_health(client, app)
    assert len(app.config["_SUMMARY_CACHE"]["all_entries"]) == 2

    with db.connect(str(tmp_path / "test.db")) as conn:
        removed_id = next(iter(db.get_report_ids(conn) - {keep.report_id}))
        db.delete_report(conn, removed_id)

    with patch.object(db, "load_all_entries", wraps=db.load_all_entries) as spy_full:
        _hit_import_health(client, app)
    spy_full.assert_called_once()

    cache = app.config["_SUMMARY_CACHE"]
    assert len(cache["all_entries"]) == 1
    assert set(cache["all_entries"]["sku"]) == {"B"}


def test_a_settings_only_change_reuses_all_entries_and_only_recomputes_summary(app_and_client, tmp_path):
    app, client = app_and_client
    with db.connect(str(tmp_path / "test.db")) as conn:
        db.import_report(conn, _df("A"), _meta("2026-06-01", "2026-06-01"), "day1.xlsx")

    _hit_import_health(client, app)
    cache_before = app.config["_SUMMARY_CACHE"]
    frame_before = cache_before["all_entries"]

    resp = client.post(
        "/api/settings",
        json={
            "low_stock_days": 20,
            "overstock_days": 90,
            "trailing_days_target": 90,
            "dead_stock_days": 90,
            "value_tier_a_pct": 70,
            "value_tier_b_pct": 90,
        },
        headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]},
    )
    assert resp.status_code == 200

    with patch.object(db, "load_all_entries", wraps=db.load_all_entries) as spy_full:
        _hit_import_health(client, app)
    spy_full.assert_not_called()

    cache_after = app.config["_SUMMARY_CACHE"]
    # Same underlying data — reused, not rebuilt — but the summary and
    # settings_version reflect the new threshold.
    assert cache_after["all_entries"] is frame_before
    assert cache_after["settings"]["low_stock_days"] == 20
    assert cache_after["settings_version"] != cache_before["settings_version"]
