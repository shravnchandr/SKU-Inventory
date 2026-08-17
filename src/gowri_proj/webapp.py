"""Local web app: everything the CLI can do, driven from a browser instead.

Runs entirely on localhost — no data leaves the machine. Two pages (Dashboard,
Reports) share one nav shell; a handful of small JSON endpoints back the
upload/refresh/remove actions so the pages update without a full reload.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
from flask import Flask, abort, g, jsonify, redirect, render_template, request, url_for
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from . import db
from .analysis import (
    THRESHOLD_DAYS_MAX,
    THRESHOLD_DAYS_MIN,
    THRESHOLD_PCT_MAX,
    THRESHOLD_PCT_MIN,
    InventorySummary,
    brand_history,
    find_data_quality_issues,
    find_import_gaps,
    find_sku_churn,
    find_unmatched_skus,
    format_days_of_cover,
    search_skus,
    sku_history,
    summarize_history,
)
from .dashboard import (
    SEGMENT_MOVEMENT_LABELS,
    SEGMENT_TIER_LABELS,
    _sanitize,
    _segment_policy,
    build_payload,
)
from .parser import parse_item_list, parse_stock_statement
from .sync import DEFAULT_UPLOADS_DIR, fy_folder, sync_folder

ITEM_CATALOG_FILENAME = "item_catalog.xlsx"

DEFAULT_DB_PATH = db.DEFAULT_DB_PATH
MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB — generous for a stock statement export


def _value_segment_for(summary: InventorySummary, sku: str) -> dict | None:
    """The ABC-tier x movement segment for one SKU, or None if it isn't
    segmented (out_of_stock/returned, or not currently stocked at all —
    see analysis._compute_value_segments)."""
    rows = summary.value_segment_skus[summary.value_segment_skus["sku"] == sku]
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {
        "tier": row["tier"],
        "movement": row["movement"],
        "tier_label": SEGMENT_TIER_LABELS[row["tier"]],
        "movement_label": SEGMENT_MOVEMENT_LABELS[row["movement"]],
        "policy": _segment_policy(row["tier"], row["movement"]),
    }


def _validate_upload(file: FileStorage | None) -> tuple[str | None, tuple | None]:
    """(filename, None) for a present, non-empty .xls/.xlsx upload, or
    (None, <error response tuple>) to return straight from the route.
    Shared by /api/upload and /api/upload-item-list — both accept the same
    file shape, they just do different things with it afterward.
    """
    if not file or not file.filename:
        return None, (jsonify(error="No file received."), 400)
    filename = secure_filename(file.filename)
    if not filename.lower().endswith((".xls", ".xlsx")):
        return None, (jsonify(error="Only .xls or .xlsx files are supported."), 400)
    return filename, None


@contextmanager
def _staged_upload(file: FileStorage, filename: str, uploads_dir: Path):
    """Save `file` to a dotfile-named temp path inside uploads_dir (same
    filesystem as the eventual destination, so the final move can be an
    atomic os.replace) and yield that path — validate the saved file
    *before* it ever becomes the real, visibly-named file, so a rejected
    upload can't overwrite an existing report's/catalog's provenance.
    Always removes the temp file on the way out; a no-op if the caller
    already os.replace'd it into its final destination.
    """
    uploads_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".upload-", suffix=Path(filename).suffix, dir=uploads_dir
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            file.save(f)
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)  # no-op if os.replace already moved it into place


def _serialize_reports(reports_df: pd.DataFrame) -> list[dict]:
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

        The data half of that cache (all_entries) is the expensive part —
        db.load_all_entries scans every row of every report ever imported.
        Under monthly-cadence uploads that's cheap enough not to matter, but
        under daily-cadence uploads it's ~30x more rows for the same
        calendar span, and it was being fully re-read on *every* cache miss,
        including a miss caused by nothing more than a Settings save. When
        the only thing that changed since the last load is which reports
        exist, and every previously-cached report is still there (the normal
        case — a new day's file lands, nothing was removed), this extends
        the cached frame with just the new reports' rows instead of
        re-reading all of history. Anything else (first load, or a report
        actually removed/superseded) falls back to a full reload.
        """
        if "current_data" in g:
            return g.current_data
        with db.connect(app.config["DB_PATH"]) as conn:
            # COUNT alone can't tell a delete-then-reinsert within the same
            # replace apart from no change at all, and imported_at is only
            # second-resolution — MAX(id) is monotonic (SQLite AUTOINCREMENT
            # never reuses ids) so it catches replacements the other two miss.
            # settings.version is tracked separately (not folded into one
            # combined fingerprint) so a settings-only save can invalidate
            # the summary without forcing all_entries to be re-read too.
            row = conn.execute(
                "SELECT (SELECT COUNT(*) FROM reports), (SELECT COALESCE(MAX(id), 0) FROM reports), "
                "(SELECT COALESCE(MAX(imported_at), '') FROM reports), "
                "(SELECT COALESCE(version, 0) FROM settings WHERE id = 1)"
            ).fetchone()
            data_fingerprint, settings_version = row[:3], row[3]
            cache = app.config["_SUMMARY_CACHE"]

            if not db.has_data(conn):
                app.config["_SUMMARY_CACHE"] = {
                    "data_fingerprint": data_fingerprint,
                    "report_ids": frozenset(),
                    "all_entries": None,
                    "settings_version": settings_version,
                    "summary": None,
                    "settings": None,
                }
                g.current_data = result = None, None, None
                return result

            if cache and cache["data_fingerprint"] == data_fingerprint:
                all_entries = cache["all_entries"]
                report_ids = cache["report_ids"]
                data_changed = False
            else:
                current_ids = db.get_report_ids(conn)
                if (
                    cache
                    and cache["all_entries"] is not None
                    and cache["report_ids"] < current_ids  # a strict subset: pure addition
                ):
                    new_rows = db.load_entries_for_reports(conn, current_ids - cache["report_ids"])
                    all_entries = pd.concat([cache["all_entries"], new_rows], ignore_index=True)
                else:
                    all_entries = db.load_all_entries(conn)
                report_ids = current_ids
                data_changed = True

            if not data_changed and cache and cache["settings_version"] == settings_version:
                summary = cache["summary"]
                settings = cache["settings"]
            else:
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

            app.config["_SUMMARY_CACHE"] = {
                "data_fingerprint": data_fingerprint,
                "report_ids": report_ids,
                "all_entries": all_entries,
                "settings_version": settings_version,
                "summary": summary,
                "settings": settings,
            }
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
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith(
            "/api/"
        ):
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
        value_segment = _value_segment_for(summary, sku)
        return jsonify(
            _sanitize(
                {
                    "brand": display_brand,
                    "sku": sku,
                    "current": current,
                    "value_segment": value_segment,
                    "history": history,
                }
            )
        )

    @app.get("/api/brand-detail")
    def api_brand_detail():
        brand = request.args.get("brand", "")
        all_entries, summary, _ = get_current_data()
        if summary is None:
            return jsonify(error="No data imported yet."), 404
        history = brand_history(all_entries, brand)
        if not history:
            return jsonify(error="No history found for that brand."), 404
        skus_df = summary.enriched[summary.enriched["brand"] == brand].sort_values(
            "value", ascending=False
        )
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
        return jsonify(
            _sanitize({"brand": brand, "current": current, "history": history, "skus": skus})
        )

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
        sku_churn = (
            find_sku_churn(all_entries, alias_map)
            if all_entries is not None
            else {
                "new_skus": [],
                "new_total": 0,
                "vanished_skus": [],
                "vanished_total": 0,
                "likely_renames": [],
                "renames_total": 0,
                "previous_period_end": None,
            }
        )
        quality_issues = find_data_quality_issues(all_entries) if all_entries is not None else []
        return jsonify(
            last_refresh=last_refresh,
            missing_months=gaps["missing_months"],
            coverage_gaps=gaps["coverage_gaps"],
            problem_files=problem_files,
            item_catalog=catalog_meta,
            unmatched_skus=unmatched_skus,
            sku_churn=sku_churn,
            # Impossible/inconsistent source rows (see analysis.py's
            # find_data_quality_issues) — capped defensively, same reasoning
            # as unmatched_skus's display limit; "total" still reflects the
            # true count so the headline can't understate the problem.
            quality_issues={"total": len(quality_issues), "items": quality_issues[:100]},
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
        filename, error = _validate_upload(file)
        if error:
            return error

        uploads_dir = Path(app.config["UPLOADS_DIR"])
        with _staged_upload(file, filename, uploads_dir) as tmp_path:
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
                if existing_report_id is not None and (
                    known is None or known["report_id"] != existing_report_id
                ):
                    return jsonify(
                        error=f"That period ({meta.period_start} to {meta.period_end}) is already imported "
                        f"under a different filename."
                    ), 409

                # Validated — now it's safe to make this the real file. Any
                # overlap with an existing report (not just exact-period or
                # reused-filename, both already checked above) is resolved
                # by import_report itself, deleting the old overlapping
                # report(s) — the newest upload always wins.
                dest = uploads_dir / rel_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, dest)
                stat = dest.stat()
                import_result = db.import_report(
                    conn, df, meta, rel_name, replace=(existing_report_id is not None)
                )
                db.upsert_watched_file(
                    conn,
                    rel_name,
                    stat.st_size,
                    int(stat.st_mtime),
                    import_result.report_id,
                    "imported",
                )

            return jsonify(
                status="imported",
                period_start=meta.period_start.isoformat(),
                period_end=meta.period_end.isoformat(),
                sku_count=import_result.sku_count,
                superseded_report_ids=import_result.superseded_report_ids,
            )

    @app.post("/api/upload-item-list")
    def api_upload_item_list():
        file = request.files.get("file")
        filename, error = _validate_upload(file)
        if error:
            return error

        uploads_dir = Path(app.config["UPLOADS_DIR"])
        # Same stage-then-atomic-replace pattern as /api/upload — validate
        # before this can overwrite the last known-good catalog. Unlike
        # monthly reports, there's only ever one "current" catalog (no
        # period, no FY folder), so it always lands at the same canonical
        # path and each successful upload unconditionally replaces it.
        with _staged_upload(file, filename, uploads_dir) as tmp_path:
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
                previous_catalog = db.get_item_catalog_df(conn)
                name_changes_watermark = db.get_item_name_changes_watermark(conn)
                item_count = db.import_item_catalog(conn, df)

            dest = uploads_dir / ITEM_CATALOG_FILENAME
            try:
                os.replace(tmp_path, dest)
            except OSError as e:
                # The DB import above already committed — both the catalog
                # itself and any rename it detected (item_name_changes) — if
                # writing the file back out fails (disk full, permissions,
                # ...), roll both back to the pre-upload snapshot so the DB
                # still matches what the still-intact file on disk actually
                # says, rather than disagreeing with it in the other
                # direction, and so a rename that never actually took effect
                # doesn't linger as if it had.
                with db.connect(app.config["DB_PATH"]) as conn:
                    db.rollback_item_catalog(conn, previous_catalog, name_changes_watermark)
                return jsonify(
                    error=f"Could not save the uploaded file ({e}). The catalog was not changed."
                ), 500

            return jsonify(
                status="imported",
                item_count=item_count,
                as_of=meta.as_of.isoformat() if meta.as_of else None,
            )

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
