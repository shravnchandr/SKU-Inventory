"""Tests for analysis.status_history (the dashboard's history popups) and
its /api/status-history endpoint.

status_history is summarize_history() re-run as of each past month, so the
key guarantee is equality: its latest point must match the live dashboard's
summary exactly, and every earlier point must match summarize_history run
over just the reports that existed by then.
"""

from datetime import date

import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.analysis import STATUS_ORDER, status_history, summarize_history
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.routes import inventory
from src.gowri_proj.webapp import create_app

MONTHS = [
    ("2026-01-01", "2026-01-31"),
    ("2026-02-01", "2026-02-28"),
    ("2026-03-01", "2026-03-31"),
    ("2026-04-01", "2026-04-30"),
    ("2026-05-01", "2026-05-31"),
]


def _rows(month_idx):
    # A mix that moves between statuses over time: FAST sells steadily;
    # IDLE has stock but never sells (overstock early, dead once 90 days
    # pass); SOLDOUT runs out in the last month; LOW sells nearly all it has.
    rows = [
        ("FAST", 50, 500.0, 30),
        ("IDLE", 20, 200.0, 0),
        ("SOLDOUT", 0 if month_idx == len(MONTHS) - 1 else 10, 100.0, 10),
        ("LOW", 2, 20.0, 40),
    ]
    return pd.DataFrame(
        [
            {
                "brand": "BRAND", "sku": sku, "opening_stock": closing + sales, "purchase": 0.0,
                "purchase_free": 0.0, "other_receipt": 0.0, "sales": sales, "sales_free": 0.0,
                "other_issue": 0.0, "closing_stock": closing,
                "value": value if closing else 0.0,
            }
            for sku, closing, value, sales in rows
        ],
        columns=TIDY_COLUMNS,
    )


def _meta(start, end):
    return ReportMeta(
        company="TEST PHARMACY", location="TEST CITY",
        period_start=date.fromisoformat(start), period_end=date.fromisoformat(end),
    )


def _seed(db_path, months=MONTHS):
    with db.connect(db_path) as conn:
        for i, (start, end) in enumerate(months):
            db.import_report(conn, _rows(i), _meta(start, end), f"{end}.xls")


@pytest.fixture
def entries(tmp_path):
    path = str(tmp_path / "t.db")
    _seed(path)
    with db.connect(path) as conn:
        return db.load_all_entries(conn)


def _point_matches(point, summary):
    assert point["status_counts"] == {s: summary.status_counts.get(s, 0) for s in STATUS_ORDER}
    for s in STATUS_ORDER:
        assert point["status_values"][s] == pytest.approx(summary.status_values.get(s, 0.0))
    assert point["total_skus"] == summary.total_skus
    assert point["total_value"] == pytest.approx(summary.total_value)
    assert point["total_units"] == pytest.approx(summary.total_units)


def test_latest_point_is_exactly_the_live_dashboard(entries):
    points = status_history(entries)
    _point_matches(points[-1], summarize_history(entries))


def test_each_earlier_point_matches_summarize_history_as_of_that_month(entries):
    points = status_history(entries)
    assert [p["as_of"] for p in points] == [end for _, end in MONTHS]
    for point in points:
        as_of = pd.Timestamp(point["as_of"])
        _point_matches(point, summarize_history(entries[entries["period_end"] <= as_of]))


def test_history_actually_moves_between_statuses(entries):
    # Guards against a history that's accidentally the same point repeated.
    points = status_history(entries)
    assert points[0]["status_counts"]["dead_stock"] == 0
    assert points[-1]["status_counts"]["dead_stock"] == 1  # IDLE, once 90+ days pass
    assert points[-1]["status_counts"]["out_of_stock"] == 1  # SOLDOUT, last month only
    assert points[-2]["status_counts"]["out_of_stock"] == 0


def test_thresholds_are_passed_through(entries):
    default = status_history(entries)[-1]
    strict = status_history(entries, dead_stock_days=365)[-1]
    assert default["status_counts"]["dead_stock"] == 1
    assert strict["status_counts"]["dead_stock"] == 0


def test_approximate_flags_cover_only_the_early_months(entries):
    points = status_history(entries)
    # Jan: 31 days of history — short of both the 90-day sales window and
    # the 90-day dead-stock threshold.
    assert points[0]["short_sales_window"] and points[0]["dead_stock_unmeasurable"]
    # Mar: 90 days imported (Jan 1 – Mar 31) — both now fully measurable.
    assert not points[2]["short_sales_window"]
    assert not points[2]["dead_stock_unmeasurable"]
    assert not any(p["short_sales_window"] or p["dead_stock_unmeasurable"] for p in points[2:])


def test_one_point_per_month_using_that_months_latest_report(tmp_path):
    # Daily-style cadence within May: three reports, one month point, and it
    # must be the latest of the three (May 3), labeled as a partial month.
    path = str(tmp_path / "t.db")
    _seed(path, MONTHS[:4] + [("2026-05-01", "2026-05-01"), ("2026-05-02", "2026-05-02"),
                              ("2026-05-03", "2026-05-03")])
    with db.connect(path) as conn:
        entries = db.load_all_entries(conn)
    points = status_history(entries)
    assert [p["as_of"] for p in points][-2:] == ["2026-04-30", "2026-05-03"]
    assert len(points) == 5
    assert points[-1]["label"] == "3 May 2026"
    assert points[-2]["label"] == "Apr 2026"
    _point_matches(points[-1], summarize_history(entries))


def test_empty_entries_give_empty_history():
    assert status_history(pd.DataFrame(columns=["report_id", "period_start", "period_end"])) == []


# ---------- /api/status-history ----------


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    with app.test_client() as c:
        yield c, app


def test_endpoint_404_with_no_data(client):
    c, _ = client
    assert c.get("/api/status-history").status_code == 404


def test_endpoint_returns_points_matching_the_dashboard(client):
    c, app = client
    _seed(app.config["DB_PATH"])
    data = c.get("/api/status-history").get_json()
    assert len(data["points"]) == len(MONTHS)
    assert data["trailing_days_target"] == 90
    assert data["dead_stock_days"] == 90
    with db.connect(app.config["DB_PATH"]) as conn:
        _point_matches(data["points"][-1], summarize_history(db.load_all_entries(conn)))


def test_endpoint_computes_once_then_recomputes_after_new_data(client, monkeypatch):
    c, app = client
    _seed(app.config["DB_PATH"], MONTHS[:4])
    calls = []
    real = inventory.status_history
    monkeypatch.setattr(inventory, "status_history", lambda *a, **k: calls.append(1) or real(*a, **k))

    first = c.get("/api/status-history").get_json()
    c.get("/api/status-history")
    assert len(calls) == 1  # second request served from cache

    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(conn, _rows(4), _meta(*MONTHS[4]), "may.xls")
    second = c.get("/api/status-history").get_json()
    assert len(calls) == 2  # new import invalidated it
    assert len(second["points"]) == len(first["points"]) + 1


def test_endpoint_recomputes_after_settings_change(client):
    c, app = client
    _seed(app.config["DB_PATH"])
    assert c.get("/api/status-history").get_json()["points"][-1]["status_counts"]["dead_stock"] == 1
    with db.connect(app.config["DB_PATH"]) as conn:
        db.upsert_settings(conn, 15, 90, 90, 365, 70, 90)
    data = c.get("/api/status-history").get_json()
    assert data["dead_stock_days"] == 365
    assert data["points"][-1]["status_counts"]["dead_stock"] == 0


def test_dashboard_has_history_buttons_and_clickable_kpis(client):
    c, app = client
    _seed(app.config["DB_PATH"])
    html = c.get("/dashboard").data.decode()
    assert "/api/status-history" in html
    assert "history-btn" in html
    assert 'data-history="${k.history}"' in html
