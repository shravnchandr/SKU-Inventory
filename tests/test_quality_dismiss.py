"""The data-quality warning's "Dismiss" on the Reports page is remembered in
the browser against a fingerprint of the exact issue set, served by
/api/import-health. These cover that fingerprint: it must change whenever
the set of issues changes (or a dismissal would hide new problems) and stay
put when nothing did (or a dismissal wouldn't stick)."""

from datetime import date

import pandas as pd
import pytest

from src.gowri_proj import db
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.webapp import create_app, quality_issues_fingerprint


def _issue(sku, issue="Negative sales (possibly a return)", period_end="2026-06-30"):
    return {"brand": "B", "sku": sku, "period_end": period_end, "issue": issue}


def test_fingerprint_is_none_when_there_are_no_issues():
    assert quality_issues_fingerprint([]) is None


def test_fingerprint_ignores_order():
    a, b = _issue("X"), _issue("Y")
    assert quality_issues_fingerprint([a, b]) == quality_issues_fingerprint([b, a])


def test_fingerprint_changes_when_one_issue_is_swapped_for_another():
    # Same count, different issue: a dismissal must not carry over.
    before = quality_issues_fingerprint([_issue("X"), _issue("Y")])
    after = quality_issues_fingerprint([_issue("X"), _issue("Z")])
    assert before != after


def test_fingerprint_changes_when_an_issue_is_added_or_its_kind_changes():
    base = quality_issues_fingerprint([_issue("X")])
    assert quality_issues_fingerprint([_issue("X"), _issue("Y")]) != base
    assert quality_issues_fingerprint([_issue("X", issue="Negative closing stock")]) != base
    assert quality_issues_fingerprint([_issue("X", period_end="2026-07-31")]) != base


def _row(sku, sales):
    return {
        "brand": "B", "sku": sku, "opening_stock": 5.0, "purchase": 0.0, "purchase_free": 0.0,
        "other_receipt": 0.0, "sales": sales, "sales_free": 0.0, "other_issue": 0.0,
        "closing_stock": 5.0 - sales, "value": 50.0,
    }


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "test.db"), uploads_dir=str(tmp_path / "uploads"))
    with app.test_client() as c:
        yield c, app


def _import(app, rows, start, end):
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(
            conn, pd.DataFrame(rows, columns=TIDY_COLUMNS),
            ReportMeta("T", "T", date.fromisoformat(start), date.fromisoformat(end)), f"{end}.xls",
        )  # fmt: skip


def test_import_health_serves_a_fingerprint_that_tracks_the_issue_set(client):
    c, app = client
    _import(app, [_row("OK", 1.0), _row("BAD", -2.0)], "2026-06-01", "2026-06-30")
    first = c.get("/api/import-health").get_json()["quality_issues"]
    assert first["total"] == 1
    assert first["fingerprint"]
    # Unchanged data: same fingerprint, so a dismissal sticks across visits.
    assert c.get("/api/import-health").get_json()["quality_issues"]["fingerprint"] == first[
        "fingerprint"
    ]
    # A new month with a new bad row: fingerprint changes, warning returns.
    _import(app, [_row("OK", 1.0), _row("BAD2", -1.0)], "2026-07-01", "2026-07-31")
    second = c.get("/api/import-health").get_json()["quality_issues"]
    assert second["fingerprint"] != first["fingerprint"]


def test_reports_page_has_the_dismiss_wiring(client):
    c, app = client
    _import(app, [_row("OK", 1.0)], "2026-06-01", "2026-06-30")
    html = c.get("/reports").data.decode()
    assert "inv-quality-dismissed" in html
    assert 'id="quality-dismiss"' in html
