"""The Dashboard warns when the newest imported data is over a month old."""

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from src.gowri_proj import db, webapp
from src.gowri_proj.parser import TIDY_COLUMNS, ReportMeta
from src.gowri_proj.webapp import create_app


def _client(tmp_path, end: date):
    app = create_app(db_path=str(tmp_path / "t.db"), uploads_dir=str(tmp_path / "u"))
    row = {"brand": "B", "sku": "X", "opening_stock": 5.0, "purchase": 0.0, "purchase_free": 0.0,
           "other_receipt": 0.0, "sales": 1.0, "sales_free": 0.0, "other_issue": 0.0,
           "closing_stock": 4.0, "value": 40.0}  # fmt: skip
    with db.connect(app.config["DB_PATH"]) as conn:
        db.import_report(conn, pd.DataFrame([row], columns=TIDY_COLUMNS),
                         ReportMeta("T", "T", end - timedelta(days=29), end), "r.xls")  # fmt: skip
    return app.test_client()


@pytest.mark.parametrize(
    "age, expected", [(10, 10), (webapp.STALE_DATA_DAYS + 10, webapp.STALE_DATA_DAYS + 10)]
)
def test_payload_carries_age_of_latest_data(tmp_path, age, expected):
    today = datetime.now(UTC).astimezone().date()
    html = _client(tmp_path, today - timedelta(days=age)).get("/dashboard").data.decode()
    assert f'"days_since_latest": {expected}' in html
    assert f'"stale_after_days": {webapp.STALE_DATA_DAYS}' in html
    assert 'id="stale-strip"' in html
