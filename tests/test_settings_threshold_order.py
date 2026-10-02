"""Low stock must be fewer days of cover than overstock — otherwise nothing
can ever be "healthy" between them (low stock is checked first). The
Settings page used to save such a combination without complaint."""

import subprocess
import sys

import pytest

from src.gowri_proj.webapp import create_app

VALID = {"low_stock_days": 15, "overstock_days": 90, "trailing_days_target": 90,
         "dead_stock_days": 90, "value_tier_a_pct": 70, "value_tier_b_pct": 90}  # fmt: skip


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=str(tmp_path / "t.db"), uploads_dir=str(tmp_path / "u"))
    return app.test_client(), {"X-CSRF-Token": app.config["CSRF_TOKEN"]}


@pytest.mark.parametrize("low, over", [(60, 30), (45, 45)])
def test_low_at_or_above_overstock_is_rejected(client, low, over):
    c, h = client
    r = c.post(
        "/api/settings", json=VALID | {"low_stock_days": low, "overstock_days": over}, headers=h
    )
    assert r.status_code == 400
    assert "must be fewer days of cover than overstock" in r.get_json()["error"]


def test_valid_settings_still_save(client):
    c, h = client
    assert c.post("/api/settings", json=VALID, headers=h).status_code == 200


def test_cli_override_that_contradicts_is_rejected(tmp_path):
    from datetime import date

    import pandas as pd

    from src.gowri_proj import db
    from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta

    row = {c: 0.0 for c in TIDY_COLUMNS} | {"brand": "B", "sku": "X", "closing_stock": 1.0}
    with db.connect(str(tmp_path / "t.db")) as conn:
        db.import_report(conn, pd.DataFrame([row], columns=TIDY_COLUMNS),
                         ReportMeta("T", "T", date(2026, 7, 1), date(2026, 7, 31)), "jul.xls")  # fmt: skip
    r = subprocess.run(
        [sys.executable, "main.py", "--db", str(tmp_path / "t.db"), "dashboard", "--out", str(tmp_path / "d.html"),
         "--low-stock-days", "100", "--overstock-days", "90"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode != 0
    assert "must be fewer days of cover than overstock" in r.stderr
    assert "Traceback" not in r.stderr
