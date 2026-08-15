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
    THRESHOLD_PCT_MAX,
    THRESHOLD_PCT_MIN,
    brand_history,
    find_import_gaps,
    find_sku_churn,
    find_unmatched_skus,
    format_days_of_cover,
    search_skus,
    sku_history,
    summarize_history,
)
from .dashboard import _sanitize, build_payload
from .parser import parse_item_list, parse_stock_statement
from .sync import fy_folder, sync_folder

ITEM_CATALOG_FILENAME = "item_catalog.xlsx"

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
                    value_tier_a_pct=settings["value_tier_a_pct"],
                    value_tier_b_pct=settings["value_tier_b_pct"],
                )
                app.config["_SUMMARY_CACHE"] = (fingerprint, all_entries, summary, settings)
                result = all_entries, summary, settings
        g.current_data = result
        return result

    def company_name() -> str | None:
        # Reuse the route's own get_current_data() call for free if it
        # already ran this request (Dashboard/Trends always call it) — but
        # don't trigger get_current_data() ourselves just to answer this.
        # Reports/Settings don't need a full summarize_history() over every
        # SKU (the expensive part, and the thing invalidated on every
        # upload/refresh/remove/settings-save — exactly the actions that
        # land back on these two pages) merely to print a company name in
        # the nav bar; list_reports() is ~180x cheaper for that alone.
        if "current_data" in g:
            _, summary, _ = g.current_data
            return summary.meta.company if summary else None
        with db.connect(app.config["DB_PATH"]) as conn:
            reports = db.list_reports(conn)
        if reports.empty:
            return None
        return reports.iloc[-1]["company"]

    @app.context_processor
    def inject_globals():
        return {"company": company_name(), "csrf_token": app.config["CSRF_TOKEN"]}

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
            catalog_meta = db.get_item_catalog_meta(conn)
            catalog_names = db.list_item_catalog_names(conn) if catalog_meta else set()
            alias_map = db.get_name_change_map(conn)
        problem_files = [
            {
                "filename": r["filename"],
                "status": r["status"],
                "detail": r["detail"],
                "checked_at": str(r["checked_at"]),
            }
            for _, r in problems_df.iterrows()
        ]
        all_entries, summary, _ = get_current_data()
        unmatched_skus = {"total": 0, "items": []}
        if catalog_names and summary is not None:
            unmatched_skus = find_unmatched_skus(summary.enriched, catalog_names)
        sku_churn = find_sku_churn(all_entries, alias_map) if all_entries is not None else {
            "new_skus": [], "new_total": 0, "vanished_skus": [], "vanished_total": 0,
            "likely_renames": [], "renames_total": 0, "previous_period_end": None,
        }
        return jsonify(
            last_refresh=last_refresh,
            missing_months=gaps["missing_months"],
            coverage_gaps=gaps["coverage_gaps"],
            problem_files=problem_files,
            item_catalog=catalog_meta,
            unmatched_skus=unmatched_skus,
            sku_churn=sku_churn,
        )

    @app.get("/settings")
    def settings():
        with db.connect(app.config["DB_PATH"]) as conn:
            current_settings = db.get_settings(conn)
        return render_template("settings.html", active_page="settings", settings=current_settings)

    @app.post("/api/settings")
    def api_save_settings():
        body = request.get_json(silent=True) or {}
        day_fields = ["low_stock_days", "overstock_days", "trailing_days_target", "dead_stock_days"]
        pct_fields = ["value_tier_a_pct", "value_tier_b_pct"]
        values = {}
        for field_name in day_fields + pct_fields:
            raw = body.get(field_name)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return jsonify(error=f"'{field_name}' must be a whole number."), 400
            if field_name in day_fields:
                if not (THRESHOLD_DAYS_MIN <= value <= THRESHOLD_DAYS_MAX):
                    return jsonify(
                        error=f"'{field_name}' must be between {THRESHOLD_DAYS_MIN} and {THRESHOLD_DAYS_MAX} days."
                    ), 400
            else:
                if not (THRESHOLD_PCT_MIN <= value <= THRESHOLD_PCT_MAX):
                    return jsonify(
                        error=f"'{field_name}' must be between {THRESHOLD_PCT_MIN} and {THRESHOLD_PCT_MAX} percent."
                    ), 400
            values[field_name] = value
        if values["value_tier_a_pct"] >= values["value_tier_b_pct"]:
            return jsonify(error="'value_tier_a_pct' must be less than 'value_tier_b_pct'."), 400
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

        # Stage the upload under a dotfile name in uploads_dir itself (same
        # filesystem as the eventual destination, so the final move is an
        # atomic replace) and validate it there *before* it ever becomes a
        # real file — writing straight to the real filename first would let
        # a rejected upload (conflicting period, duplicate, bad file)
        # overwrite the existing report's provenance on every attempt.
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

            # File it under the same financial-year subfolder a manual
            # upload/rescan would use (uploads/2627/..., matching every
            # historical file already organized that way) instead of
            # dropping it flat in uploads/ — and track it under that same
            # relative path, so a later "Rescan uploads folder" recognizes
            # it as the file it already knows about rather than a new one.
            rel_name = f"{fy_folder(meta.period_start)}/{filename}"

            with db.connect(app.config["DB_PATH"]) as conn:
                known = db.get_watched_file(conn, rel_name)
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
                dest = uploads_dir / rel_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, dest)
                stat = dest.stat()
                import_result = db.import_report(
                    conn, df, meta, rel_name, replace=(existing_report_id is not None)
                )
                db.upsert_watched_file(
                    conn, rel_name, stat.st_size, int(stat.st_mtime), import_result.report_id, "imported"
                )

            return jsonify(
                status="imported",
                period_start=meta.period_start.isoformat(),
                period_end=meta.period_end.isoformat(),
                sku_count=import_result.sku_count,
            )
        finally:
            tmp_path.unlink(missing_ok=True)  # no-op if os.replace already moved it into place

    @app.post("/api/upload-item-list")
    def api_upload_item_list():
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify(error="No file received."), 400
        filename = secure_filename(file.filename)
        if not filename.lower().endswith((".xls", ".xlsx")):
            return jsonify(error="Only .xls or .xlsx files are supported."), 400

        uploads_dir = Path(app.config["UPLOADS_DIR"])
        uploads_dir.mkdir(parents=True, exist_ok=True)

        # Same stage-then-atomic-replace pattern as /api/upload — validate
        # before this can overwrite the last known-good catalog. Unlike
        # monthly reports, there's only ever one "current" catalog (no
        # period, no FY folder), so it always lands at the same canonical
        # path and each successful upload unconditionally replaces it.
        fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=Path(filename).suffix, dir=uploads_dir)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as f:
                file.save(f)

            try:
                df, meta = parse_item_list(str(tmp_path))
            except Exception as e:  # noqa: BLE001 — surfaced to the user, not a server error
                return jsonify(error=str(e)), 422
            if df.empty:
                return jsonify(
                    error="No item rows could be read from this file — it may not be an item list export."
                ), 422

            # Import to the DB before touching the saved file — if the
            # import fails (e.g. a duplicate code), the canonical file on
            # disk should still match what's actually in the DB, not a
            # newer file the import never actually accepted.
            with db.connect(app.config["DB_PATH"]) as conn:
                item_count = db.import_item_catalog(conn, df)

            dest = uploads_dir / ITEM_CATALOG_FILENAME
            os.replace(tmp_path, dest)

            return jsonify(
                status="imported",
                item_count=item_count,
                as_of=meta.as_of.isoformat() if meta.as_of else None,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

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
