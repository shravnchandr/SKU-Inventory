"""The Reports page's "Renamed items" review, end to end through the web app:
listing suggestions and automatic merges, approving / rejecting / splitting,
undo, and every page picking a decision up without a restart."""

from datetime import date

import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.webapp import create_app


def _row(sku, opening, closing, sales=0.0, other_issue=0.0):
    return {
        "brand": "BRAND", "sku": sku, "opening_stock": opening, "purchase": 0.0,
        "purchase_free": 0.0, "other_receipt": 0.0, "sales": sales, "sales_free": 0.0,
        "other_issue": other_issue, "closing_stock": closing, "value": closing * 10.0,
    }  # fmt: skip


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    with db.connect(app.config["DB_PATH"]) as conn:
        for start, end, rows in [
            ("2026-01-01", "2026-01-31", [_row("DOLO 200MG TAB", 20, 15, 5), _row("EPIVAL TAB", 90, 80, 10)]),
            ("2026-02-01", "2026-02-28", [_row("DOLO 800MG TAB", 15, 10, 5), _row("EPIVAL TAB (NON)", 80, 80)]),
        ]:  # fmt: skip
            db.import_report(
                conn, pd.DataFrame(rows, columns=TIDY_COLUMNS),
                ReportMeta("T", "T", date.fromisoformat(start), date.fromisoformat(end)), f"{end}.xls",
            )  # fmt: skip
    with app.test_client() as c:
        yield c, app


def _post(c, app, path, body):
    return c.post(path, json=body, headers={"X-CSRF-Token": app.config["CSRF_TOKEN"]})


def test_lists_the_automatic_merge_and_the_suggestion(client):
    c, _ = client
    data = c.get("/api/sku-merges").get_json()
    assert [(m["old_name"], m["new_name"], m["reason"]) for m in data["merged"]] == [
        ("EPIVAL TAB", "EPIVAL TAB (NON)", "pharmacy tag")
    ]
    assert [(s["old_name"], s["new_name"]) for s in data["pending"]] == [
        ("DOLO 200MG TAB", "DOLO 800MG TAB")
    ]
    assert data["decided"] == []


def test_approve_then_undo(client):
    c, app = client
    assert c.get("/api/sku-detail?brand=BRAND&sku=DOLO%20800MG%20TAB").get_json()["aliases"] == []

    resp = _post(c, app, "/api/sku-merges",
                 {"old_name": "DOLO 200MG TAB", "new_name": "DOLO 800MG TAB", "decision": "merge"})  # fmt: skip
    assert resp.status_code == 200
    data = c.get("/api/sku-merges").get_json()
    assert data["pending"] == []
    assert data["decided"][0]["effect"] == "merged"
    # Every page sees it straight away — no restart, no re-import.
    detail = c.get("/api/sku-detail?brand=BRAND&sku=DOLO%20800MG%20TAB").get_json()
    assert detail["aliases"] == ["DOLO 200MG TAB"]
    assert len(detail["history"]) == 2

    assert _post(c, app, "/api/sku-merges/undo",
                 {"old_name": "DOLO 200MG TAB", "new_name": "DOLO 800MG TAB"}).get_json()["status"] == "undone"  # fmt: skip
    data = c.get("/api/sku-merges").get_json()
    assert len(data["pending"]) == 1 and data["decided"] == []


def test_reject_suggestion(client):
    c, app = client
    _post(c, app, "/api/sku-merges",
          {"old_name": "DOLO 200MG TAB", "new_name": "DOLO 800MG TAB", "decision": "separate"})  # fmt: skip
    data = c.get("/api/sku-merges").get_json()
    assert data["pending"] == []
    assert data["decided"][0]["decision"] == "separate"


def test_split_an_automatic_merge(client):
    c, app = client
    assert c.get("/api/sku-detail?brand=BRAND&sku=EPIVAL%20TAB%20(NON)").get_json()["aliases"] == [
        "EPIVAL TAB"
    ]
    _post(c, app, "/api/sku-merges",
          {"old_name": "EPIVAL TAB", "new_name": "EPIVAL TAB (NON)", "decision": "separate"})  # fmt: skip
    assert c.get("/api/sku-merges").get_json()["merged"] == []
    assert c.get("/api/sku-detail?brand=BRAND&sku=EPIVAL%20TAB%20(NON)").get_json()["aliases"] == []


def test_decision_requires_csrf_token(client):
    c, _ = client
    resp = c.post("/api/sku-merges", json={"old_name": "A", "new_name": "B", "decision": "merge"})
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"old_name": "DOLO 200MG TAB", "new_name": "DOLO 800MG TAB", "decision": "maybe"},
        {"old_name": "DOLO 200MG TAB", "decision": "merge"},
        {"old_name": "DOLO 200MG TAB", "new_name": "DOLO 200MG TAB", "decision": "merge"},
        {"old_name": 5, "new_name": "DOLO 800MG TAB", "decision": "merge"},
    ],
)
def test_bad_decision_requests_are_rejected(client, body):
    c, app = client
    assert _post(c, app, "/api/sku-merges", body).status_code == 400


def test_unknown_names_are_rejected(client):
    c, app = client
    resp = _post(
        c,
        app,
        "/api/sku-merges",
        {"old_name": "NOPE", "new_name": "DOLO 800MG TAB", "decision": "merge"},
    )
    assert resp.status_code == 404


def test_search_finds_an_item_by_its_old_name(client):
    c, _ = client
    results = c.get("/api/search?q=epival").get_json()["results"]
    assert [r["sku"] for r in results] == ["EPIVAL TAB (NON)"]
    # The old name alone (without the tag) still finds it.
    assert [r["sku"] for r in c.get("/api/search?q=EPIVAL%20TAB").get_json()["results"]] == [
        "EPIVAL TAB (NON)"
    ]


def test_import_health_counts_renames(client):
    c, _ = client
    assert c.get("/api/import-health").get_json()["renames"] == {"merged": 1, "pending": 1}


def test_renamed_item_is_not_reported_as_new_and_vanished(client):
    c, _ = client
    churn = c.get("/api/import-health").get_json()["sku_churn"]
    names = {r["sku"] for r in churn["new_skus"] + churn["vanished_skus"]}
    assert "EPIVAL TAB" not in names and "EPIVAL TAB (NON)" not in names
    # The Dolo pair is still waiting for a decision, so it does show.
    assert {"DOLO 200MG TAB", "DOLO 800MG TAB"} <= names


def test_export_lists_merges_and_pending(client):
    c, _ = client
    text = c.get("/api/sku-merges/export.csv").data.decode("utf-8-sig")
    assert "Merged,EPIVAL TAB,EPIVAL TAB (NON),pharmacy tag" in text
    assert "Waiting for review,DOLO 200MG TAB,DOLO 800MG TAB" in text


def test_review_page_has_the_card_and_reports_links_to_it(client):
    c, _ = client
    html = c.get("/review").data.decode()
    assert 'id="card-renames"' in html and "/api/sku-merges" in html
    reports = c.get("/reports").data.decode()
    assert 'id="card-renames"' not in reports  # moved, not duplicated


def test_review_count_for_the_nav_badge(client):
    c, app = client
    pending = len(c.get("/api/sku-merges").get_json()["pending"])
    assert pending > 0
    assert c.get("/api/review-count").get_json() == {"pending": pending}
    first = c.get("/api/sku-merges").get_json()["pending"][0]
    _post(c, app, "/api/sku-merges",
          {"old_name": first["old_name"], "new_name": first["new_name"], "decision": "separate"})  # fmt: skip
    assert c.get("/api/review-count").get_json() == {"pending": pending - 1}


def test_every_page_has_the_review_tab(client):
    c, _ = client
    for path in ("/dashboard", "/trends", "/reports", "/settings", "/review"):
        html = c.get(path).data.decode()
        assert 'id="nav-review-badge"' in html, path


def test_review_count_with_no_data(tmp_path):
    app = create_app(db_path=str(tmp_path / "empty.db"), uploads_dir=str(tmp_path / "u"))
    assert app.test_client().get("/api/review-count").get_json() == {"pending": 0}


def test_review_page_has_keyboard_shortcuts(client):
    c, _ = client
    html = c.get("/review").data.decode()
    assert "kb-hint" in html and "document.addEventListener('keydown'" in html
