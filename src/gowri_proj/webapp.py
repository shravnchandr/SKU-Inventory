"""Local web app: everything the CLI can do, driven from a browser instead.

Runs entirely on localhost — no data leaves the machine. Two pages (Dashboard,
Reports) share one nav shell; a handful of small JSON endpoints back the
upload/refresh/remove actions so the pages update without a full reload.
"""
from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from . import db
from .analysis import (
    THRESHOLD_DAYS_MAX,
    THRESHOLD_DAYS_MIN,
    brand_history,
    find_data_quality_issues,
    find_import_gaps,
    format_days_of_cover,
    search_skus,
    sku_history,
    summarize_history,
)
from .dashboard import _sanitize, build_payload
from .parser import parse_stock_statement
from .sync import sync_folder

DEFAULT_DB_PATH = db.DEFAULT_DB_PATH
DEFAULT_UPLOADS_DIR = "uploads"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB — generous for a stock statement export


def _serialize_reports(reports_df) -> list[dict]:
    if reports_df.empty:
        return []
    out = []
    for _, r in reports_df.iterrows():
        out.append(
            {
                "id": int(r["id"]),
                "company": r["company"],
                "location": r["location"],
                "period_start": r["period_start"].date().isoformat(),
                "period_end": r["period_end"].date().isoformat(),
                "period_days": int(r["period_days"]),
                "sku_count": int(r["sku_count"]),
                "source_filename": r["source_filename"],
                "imported_at": str(r["imported_at"]),
            }
        )
    return out


def create_app(db_path: str = DEFAULT_DB_PATH, uploads_dir: str = DEFAULT_UPLOADS_DIR) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["UPLOADS_DIR"] = uploads_dir
    app.config["_SUMMARY_CACHE"] = None
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES
    # Regenerated every process start. Not persisted — its only job is to stop
    # a page from some *other* site making mutating requests to this local
    # server (a real risk: this binds to 127.0.0.1 but any tab open in the
    # same browser can still reach it). Simple form submissions from another
    # origin can't set custom headers, so requiring this one blocks that path.
    app.config["CSRF_TOKEN"] = secrets.token_hex(16)
    Path(uploads_dir).mkdir(parents=True, exist_ok=True)

    def get_current_data():
        """(all_entries, summary, settings) for the current database state.

        Cached at two levels: app.config["_SUMMARY_CACHE"] across requests
        (invalidated by a fingerprint — see below), and flask.g within a
        single request, so a route handler and the company-name context
        processor rendering the same page share one lookup instead of each
        paying for their own.
        """
        if "current_data" in g:
            return g.current_data
        with db.connect(app.config["DB_PATH"]) as conn:
            # COUNT alone can't tell a delete-then-reinsert within the same
            # replace apart from no change at all, and imported_at is only
            # second-resolution — MAX(id) is monotonic (SQLite AUTOINCREMENT
            # never reuses ids) so it catches replacements the other two miss.
            # settings.version is folded in too — saving new thresholds from
            # the Settings page has to invalidate this cache immediately, not
            # wait for the next import.
            fingerprint = conn.execute(
                "SELECT (SELECT COUNT(*) FROM reports), (SELECT COALESCE(MAX(id), 0) FROM reports), "
                "(SELECT COALESCE(MAX(imported_at), '') FROM reports), "
                "(SELECT COALESCE(version, 0) FROM settings WHERE id = 1)"
            ).fetchone()
            cache = app.config["_SUMMARY_CACHE"]
            if cache and cache[0] == fingerprint:
                result = cache[1], cache[2], cache[3]
            elif not db.has_data(conn):
                app.config["_SUMMARY_CACHE"] = (fingerprint, None, None, None)
                result = None, None, None
            else:
                all_entries = db.load_all_entries(conn)
                settings = db.get_settings(conn)
                summary = summarize_history(
                    all_entries,
                    trailing_days_target=settings["trailing_days_target"],
                    dead_stock_days=settings["dead_stock_days"],
                    low_stock_days=settings["low_stock_days"],
                    overstock_days=settings["overstock_days"],
                )
                app.config["_SUMMARY_CACHE"] = (fingerprint, all_entries, summary, settings)
                result = all_entries, summary, settings
        g.current_data = result
        return result

    @app.context_processor
    def inject_globals():
        _, summary, _ = get_current_data()
        return {"company": summary.meta.company if summary else None, "csrf_token": app.config["CSRF_TOKEN"]}

    @app.before_request
    def check_csrf():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/"):
            token = request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(token, app.config["CSRF_TOKEN"]):
                abort(403, description="Missing or invalid CSRF token.")

    @app.errorhandler(403)
    def handle_403(e):
        return jsonify(error=str(e.description) or "Forbidden."), 403

    @app.errorhandler(413)
    def handle_too_large(e):
        mb = MAX_UPLOAD_BYTES // (1024 * 1024)
        return jsonify(error=f"That file is larger than the {mb}MB limit."), 413

    @app.get("/")
    def index():
        return redirect(url_for("dashboard"))

    @app.get("/dashboard")
    def dashboard():
        _, summary, thresholds = get_current_data()
        if summary is None:
            return redirect(url_for("reports"))
        payload = _sanitize(build_payload(summary, None, thresholds))
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

    @app.get("/api/search")
    def api_search():
        q = request.args.get("q", "")
        _, summary, _ = get_current_data()
        if summary is None:
            return jsonify(results=[])
        return jsonify(results=search_skus(summary.enriched, q))

    @app.get("/api/sku-detail")
    def api_sku_detail():
        brand, sku = request.args.get("brand", ""), request.args.get("sku", "")
        all_entries, summary, _ = get_current_data()
        if summary is None:
            return jsonify(error="No data imported yet."), 404
        history = sku_history(all_entries, sku)
        if not history:
            return jsonify(error="No history found for that SKU."), 404
        # Matched on sku alone, same reasoning as sku_history above — the
        # brand query param reflects whatever label was current when the
        # user clicked, which may be stale (e.g. a search result rendered
        # before a rename lands in the next import).
        current_rows = summary.enriched[summary.enriched["sku"] == sku]
        current = None
        if not current_rows.empty:
            row = current_rows.iloc[0]
            current = {
                "closing_stock": float(row["closing_stock"]),
                "value": round(float(row["value"]), 2),
                "sales": float(row["sales"]),
                "days_of_cover": format_days_of_cover(row["days_of_cover"]),
                "status": row["status"],
            }
        # Prefer the current snapshot's brand label over the query param —
        # it's the freshest known label for this sku, and the query param
        # may be stale (see the comment on current_rows above).
        display_brand = current_rows.iloc[0]["brand"] if not current_rows.empty else brand
        return jsonify(_sanitize({"brand": display_brand, "sku": sku, "current": current, "history": history}))

    @app.get("/api/brand-detail")
    def api_brand_detail():
        brand = request.args.get("brand", "")
        all_entries, summary, _ = get_current_data()
        if summary is None:
            return jsonify(error="No data imported yet."), 404
        history = brand_history(all_entries, brand)
        if not history:
            return jsonify(error="No history found for that brand."), 404
        skus_df = summary.enriched[summary.enriched["brand"] == brand].sort_values("value", ascending=False)
        skus = [
            {
                "sku": r["sku"],
                "closing_stock": float(r["closing_stock"]),
                "value": round(float(r["value"]), 2),
                "status": r["status"],
                "days_of_cover": format_days_of_cover(r["days_of_cover"]),
            }
            for _, r in skus_df.iterrows()
        ]
        current = {
            "sku_count": len(skus),
            "total_value": round(float(skus_df["value"].sum()), 2),
            "total_closing_stock": float(skus_df["closing_stock"].sum()),
        }
        return jsonify(_sanitize({"brand": brand, "current": current, "history": history, "skus": skus}))

    @app.get("/reports")
    def reports():
        with db.connect(app.config["DB_PATH"]) as conn:
            reports_df = db.list_reports(conn)
        reports_list = _serialize_reports(reports_df)
        return render_template("reports.html", active_page="reports", reports=reports_list)

    @app.get("/api/reports")
    def api_reports():
        with db.connect(app.config["DB_PATH"]) as conn:
            reports_df = db.list_reports(conn)
        return jsonify(reports=_serialize_reports(reports_df))

    @app.get("/api/import-health")
    def api_import_health():
        with db.connect(app.config["DB_PATH"]) as conn:
            reports_df = db.list_reports(conn)
            gaps = find_import_gaps(reports_df)
            last_refresh = db.last_refresh_time(conn)
            problems_df = db.list_watched_files_problems(conn)
        problem_files = [
            {
                "filename": r["filename"],
                "status": r["status"],
                "detail": r["detail"],
                "checked_at": str(r["checked_at"]),
            }
            for _, r in problems_df.iterrows()
        ]
        return jsonify(
            last_refresh=last_refresh,
            missing_months=gaps["missing_months"],
            coverage_gaps=gaps["coverage_gaps"],
            problem_files=problem_files,
        )

    @app.get("/api/data-quality")
    def api_data_quality():
        all_entries, summary, _ = get_current_data()
        if summary is None:
            return jsonify(issues=[])
        return jsonify(_sanitize({"issues": find_data_quality_issues(all_entries)}))

    @app.get("/settings")
    def settings():
        with db.connect(app.config["DB_PATH"]) as conn:
            current_settings = db.get_settings(conn)
        return render_template("settings.html", active_page="settings", settings=current_settings)

    @app.post("/api/settings")
    def api_save_settings():
        body = request.get_json(silent=True) or {}
        fields = ["low_stock_days", "overstock_days", "trailing_days_target", "dead_stock_days"]
        values = {}
        for field_name in fields:
            raw = body.get(field_name)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return jsonify(error=f"'{field_name}' must be a whole number."), 400
            if not (THRESHOLD_DAYS_MIN <= value <= THRESHOLD_DAYS_MAX):
                return jsonify(
                    error=f"'{field_name}' must be between {THRESHOLD_DAYS_MIN} and {THRESHOLD_DAYS_MAX} days."
                ), 400
            values[field_name] = value
        with db.connect(app.config["DB_PATH"]) as conn:
            saved = db.upsert_settings(conn, **values)
        return jsonify(saved)

    @app.post("/api/reports/<int:report_id>/remove")
    def api_remove_report(report_id: int):
        with db.connect(app.config["DB_PATH"]) as conn:
            report = db.get_report(conn, report_id)
            if report is None:
                return jsonify(error="That report no longer exists."), 404
            db.delete_report(conn, report_id)
        return jsonify(ok=True)

    @app.post("/api/upload")
    def api_upload():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify(error="No file received."), 400
        filename = secure_filename(file.filename)
        if not filename.lower().endswith((".xls", ".xlsx")):
            return jsonify(error="Only .xls or .xlsx files are supported."), 400

        uploads_dir = Path(app.config["UPLOADS_DIR"])
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Stage the upload under a dotfile name in the same directory (so the
        # eventual move is a same-filesystem, atomic replace) and validate it
        # there *before* it ever becomes uploads/<filename>. That ordering
        # matters: if we wrote straight to the real filename first, a
        # rejected upload (conflicting period, duplicate, bad file) would
        # still have overwritten whatever the existing report's provenance
        # pointed at, on every single attempt — including retries after a
        # rejection, which a "reject once" check can't catch.
        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=Path(filename).suffix, dir=uploads_dir)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                file.save(f)

            try:
                df, meta = parse_stock_statement(str(tmp_path))
            except Exception as e:  # noqa: BLE001 — surfaced to the user, not a server error
                return jsonify(error=str(e)), 422
            if not meta.period_start or not meta.period_end:
                return jsonify(error="No reporting period found in the file banner."), 422
            if df.empty:
                return jsonify(
                    error="No SKU rows could be read from this file — it may not be a stock statement export."
                ), 422

            with db.connect(app.config["DB_PATH"]) as conn:
                known = db.get_watched_file(conn, filename)
                old_report = db.find_reused_filename_conflict(conn, known, meta)
                if old_report is not None:
                    return jsonify(
                        error=f"This filename previously represented {old_report['period_start']} to "
                        f"{old_report['period_end']}; this upload is for {meta.period_start} to "
                        f"{meta.period_end}. That old report is still in the database — remove it on the "
                        f"Reports page first if this new file is correct, then re-upload."
                    ), 409

                existing_report_id = db.period_exists(conn, meta)
                if existing_report_id is not None and (known is None or known["report_id"] != existing_report_id):
                    return jsonify(
                        error=f"That period ({meta.period_start} to {meta.period_end}) is already imported "
                        f"under a different filename."
                    ), 409

                # Validated — now it's safe to make this the real file.
                dest = uploads_dir / filename
                os.replace(tmp_path, dest)
                stat = dest.stat()
                import_result = db.import_report(
                    conn, df, meta, filename, replace=(existing_report_id is not None)
                )
                db.upsert_watched_file(
                    conn, filename, stat.st_size, int(stat.st_mtime), import_result.report_id, "imported"
                )

            return jsonify(
                status="imported",
                period_start=meta.period_start.isoformat(),
                period_end=meta.period_end.isoformat(),
                sku_count=import_result.sku_count,
            )
        finally:
            tmp_path.unlink(missing_ok=True)  # no-op if os.replace already moved it into place

    @app.post("/api/refresh")
    def api_refresh():
        with db.connect(app.config["DB_PATH"]) as conn:
            result = sync_folder(conn, app.config["UPLOADS_DIR"])
        return jsonify(
            imported=result.imported,
            unchanged=result.unchanged,
            duplicate_period=result.duplicate_period,
            filename_reused=result.filename_reused,
            errors=result.errors,
        )

    return app
