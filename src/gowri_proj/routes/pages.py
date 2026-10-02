"""The HTML pages (Dashboard, Trends, Reports, Review) and the favicon."""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Flask, Response, redirect, render_template, url_for

from .. import db
from ..dashboard import (
    _sanitize,
    build_payload,
)
from ..datacache import DataCache
from ..web_helpers import (
    _serialize_reports,
)

# The Dashboard says so once the latest import is older than this — about a
# month plus a few days' grace for the next export to be made.
STALE_DATA_DAYS = 35

# Browser-tab icon: stacked boxes (inventory) in the app's accent teal
# (base.html's --accent). Inline rather than a static file — the app has no
# static/ folder, and one small string is simpler than adding one. Served
# at /favicon.ico too, since browsers request that path on their own (it
# was a 404 on every page before this existed).
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="7" fill="#0f766e"/>'
    '<rect x="7" y="17" width="8" height="8" rx="1.5" fill="#fff"/>'
    '<rect x="17" y="17" width="8" height="8" rx="1.5" fill="#fff"/>'
    '<rect x="12" y="7" width="8" height="8" rx="1.5" fill="#fff"/>'
    "</svg>"
)


def register(app: Flask, data: DataCache) -> None:
    get_current_data = data.get_current_data

    # Decorator order matters: decorators register bottom-up and url_for
    # returns the first rule registered, so the template's link resolves to
    # /favicon.svg (the honest extension for an SVG), not /favicon.ico.
    @app.get("/favicon.ico")
    @app.get("/favicon.svg")
    def favicon():
        return Response(
            FAVICON_SVG,
            mimetype="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/")
    def index():
        return redirect(url_for("dashboard"))

    @app.get("/dashboard")
    def dashboard():
        _, summary, thresholds = get_current_data()
        if summary is None:
            return redirect(url_for("reports"))
        payload = _sanitize(build_payload(summary, None, thresholds))
        # How old the newest data is, as of today on this computer — kept
        # out of build_payload (which stays a pure function of the data) and
        # added here, where "today" means something.
        today = datetime.now(UTC).astimezone().date()  # this computer's local date
        payload["meta"]["days_since_latest"] = (today - summary.meta.latest_period_end).days
        payload["meta"]["stale_after_days"] = STALE_DATA_DAYS
        return render_template("dashboard.html", active_page="dashboard", payload=payload)

    @app.get("/trends")
    def trends():
        _, summary, thresholds = get_current_data()
        if summary is None:
            return redirect(url_for("reports"))
        # Built from the same fingerprint-cached summary as /dashboard, so
        # the numbers this page shows can never disagree with the dashboard's
        # — but include_tables=False, since this page's JS never reads
        # DATA.tables (the out_of_stock/low_stock/dead_stock/overstock
        # row-level lists). Without that flag every Trends visit would
        # silently re-download the entire action-list dataset a second time
        # (~1.36MB of ~1.37MB on a real 15k-SKU database) for a page that
        # never uses it.
        payload = _sanitize(build_payload(summary, None, thresholds, include_tables=False))
        return render_template("trends.html", active_page="trends", payload=payload)

    @app.get("/reports")
    def reports():
        with db.connect(app.config["DB_PATH"]) as conn:
            reports_df = db.list_reports(conn)
        reports_list = _serialize_reports(reports_df)
        return render_template("reports.html", active_page="reports", reports=reports_list)

    @app.get("/review")
    def review():
        return render_template("review.html", active_page="review")
