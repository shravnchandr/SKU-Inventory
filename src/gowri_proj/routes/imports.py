"""Getting data in: uploads, rescans, removing reports, import health and its exports."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from flask import Flask, jsonify, request

from .. import db
from ..analysis import (
    find_data_quality_issues,
    find_import_gaps,
    find_sku_churn,
    find_unmatched_skus,
)
from ..datacache import DataCache
from ..parser import is_item_list, is_stock_statement, parse_item_list, parse_stock_statement
from ..sync import (
    ITEM_CATALOG_FILENAME,
    ITEM_LIST_STATUS,
    archive_item_list,
    fy_folder,
    prune_item_list_archive,
    sync_folder,
)
from ..web_helpers import (
    _csv_response,
    _replace_with_retry,
    _retry_transient_lock,
    _serialize_reports,
    _staged_upload,
    _validate_upload,
    item_list_reminder,
    quality_issues_fingerprint,
)


def register(app: Flask, data: DataCache) -> None:
    get_current_data = data.get_current_data
    current_identity = data.current_identity

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
        resolution = current_identity()
        return jsonify(
            # Renamed items (identity.py): how many name changes are already
            # joined up, and how many are waiting for someone to decide.
            renames={
                "merged": len(resolution.merges) if resolution else 0,
                "pending": len(resolution.suggestions) if resolution else 0,
            },
            last_refresh=last_refresh,
            missing_months=gaps["missing_months"],
            coverage_gaps=gaps["coverage_gaps"],
            problem_files=problem_files,
            item_catalog=catalog_meta,
            item_list_reminder=item_list_reminder(
                catalog_meta,
                reports_df["period_end"].max().date() if not reports_df.empty else None,
            ),
            unmatched_skus=unmatched_skus,
            sku_churn=sku_churn,
            # Impossible/inconsistent source rows (see analysis.py's
            # find_data_quality_issues) — capped defensively, same reasoning
            # as unmatched_skus's display limit; "total" still reflects the
            # true count so the headline can't understate the problem.
            # "fingerprint" identifies this exact set of issues (all of
            # them, not just the 100 shown) so a dismissal can be scoped to
            # it — see quality_issues_fingerprint.
            quality_issues={
                "total": len(quality_issues),
                "items": quality_issues[:100],
                "fingerprint": quality_issues_fingerprint(quality_issues),
            },
        )

    @app.get("/api/unmatched-skus/export")
    def api_unmatched_skus_export():
        with db.connect(app.config["DB_PATH"]) as conn:
            catalog_meta = db.get_item_catalog_meta(conn)
            catalog_names = db.list_item_catalog_names(conn) if catalog_meta else set()
        _, summary, _ = get_current_data()
        unmatched = (
            find_unmatched_skus(summary.enriched, catalog_names, limit=len(summary.enriched))
            if catalog_names and summary is not None
            else {"items": []}
        )
        return _csv_response(
            unmatched["items"],
            [
                ("brand", "Brand"),
                ("sku", "SKU"),
                ("closing_stock", "Closing stock"),
                ("value", "Value"),
            ],
            "unmatched_skus.csv",
        )

    @app.get("/api/sku-churn/export")
    def api_sku_churn_export():
        with db.connect(app.config["DB_PATH"]) as conn:
            alias_map = db.get_name_change_map(conn)
        all_entries, _, _ = get_current_data()
        churn = (
            find_sku_churn(all_entries, alias_map, limit=len(all_entries))
            if all_entries is not None and not all_entries.empty
            else {"new_skus": [], "vanished_skus": []}
        )
        rows = [{"change": "New", **r} for r in churn["new_skus"]] + [
            {"change": "Vanished", **r} for r in churn["vanished_skus"]
        ]
        return _csv_response(
            rows,
            [
                ("change", "Change"),
                ("brand", "Brand"),
                ("sku", "SKU"),
                ("closing_stock", "Closing stock"),
                ("value", "Value"),
            ],
            "sku_changes.csv",
        )

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
                if is_item_list(str(tmp_path)):
                    return jsonify(
                        error="This is the item list, not a stock statement — upload it under "
                        "Item code list, below."
                    ), 422
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
                try:
                    _replace_with_retry(tmp_path, dest)
                except OSError as e:
                    # Nothing's touched the DB yet at this point, so it's
                    # safe to just report this and stop.
                    return jsonify(error=str(e)), 500
                try:
                    stat = _retry_transient_lock(dest.stat)
                except PermissionError as e:
                    # The file *is* saved at this point (the replace above
                    # already succeeded) — just couldn't be read back for its
                    # size/mtime, most likely the same kind of transient lock
                    # as the replace itself. Nothing's touched the DB yet, so
                    # it's still safe to stop here and ask for a retry rather
                    # than import with a fingerprint we don't actually have.
                    return jsonify(
                        error=f"Saved the file but couldn't read it back afterward ({e}). "
                        "This is usually a momentary antivirus lock — try uploading again."
                    ), 500
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

                # Checked now, after the import, against whatever the latest
                # report is (this one, or a later month already imported).
                latest_end = conn.execute("SELECT MAX(period_end) FROM reports").fetchone()[0]
                reminder = item_list_reminder(
                    db.get_item_catalog_meta(conn),
                    date.fromisoformat(latest_end) if latest_end else None,
                )

            return jsonify(
                status="imported",
                period_start=meta.period_start.isoformat(),
                period_end=meta.period_end.isoformat(),
                sku_count=import_result.sku_count,
                superseded_report_ids=import_result.superseded_report_ids,
                item_list_reminder=reminder,
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
                if is_stock_statement(str(tmp_path)):
                    return jsonify(
                        error="This is a stock statement, not the item list — upload it under "
                        "Upload a new month, above."
                    ), 422
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
                _replace_with_retry(tmp_path, dest)
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
            # Only now, with both the DB import and the file save done — a
            # rolled-back upload above mustn't leave its date behind.
            with db.connect(app.config["DB_PATH"]) as conn:
                db.set_item_catalog_as_of(conn, meta.as_of)
                # Known to a rescan as the item list it already has, so it
                # isn't re-imported or flagged.
                st = dest.stat()
                db.upsert_watched_file(
                    conn, ITEM_CATALOG_FILENAME, st.st_size, int(st.st_mtime), None, ITEM_LIST_STATUS,
                    "The current item list.",
                )  # fmt: skip
                # A dated copy for the record (last 6 months kept). Not
                # worth failing an otherwise-complete upload over.
                try:
                    archive_item_list(
                        conn, uploads_dir, dest,
                        meta.as_of or datetime.now(UTC).astimezone().date(), Path(filename).suffix,
                    )  # fmt: skip
                    prune_item_list_archive(conn, uploads_dir)
                except OSError:
                    app.logger.exception("Couldn't keep a dated copy of the uploaded item list")

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
            superseded=result.superseded,
            item_lists=result.item_lists,
            item_lists_imported=result.item_lists_imported,
            errors=result.errors,
        )
