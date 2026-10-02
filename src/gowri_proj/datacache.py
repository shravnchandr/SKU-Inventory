"""The web app's view of the database: every route asks DataCache for the
current (entries, summary, settings) and the name grouping behind them,
cached across requests by a fingerprint of what they depend on (see
get_current_data)."""

from __future__ import annotations

import pandas as pd
from flask import Flask, g

from . import db, identity
from .analysis import (
    summarize_history,
)


class DataCache:
    def __init__(self, app: Flask) -> None:
        self.app = app

    def get_current_data(self):
        """(all_entries, summary, settings) for the current database state.

        Cached at two levels: self.app.config["_SUMMARY_CACHE"] across requests
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

        What's returned as all_entries is *not* the raw rows: it's them with
        renamed items' names unified (identity.apply — see identity.py), so
        every caller sees one item's whole history under one name. The raw
        frame is what's cached and extended; the unified one is rebuilt
        whenever the reports, the item list or a merge decision change
        (identity_fingerprint), and the resolution behind it is available
        to routes via current_identity().
        """
        if "current_data" in g:
            return g.current_data
        with db.connect(self.app.config["DB_PATH"]) as conn:
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
            identity_fp = db.identity_fingerprint(conn)
            cache = self.app.config["_SUMMARY_CACHE"]

            if not db.has_data(conn):
                self.app.config["_SUMMARY_CACHE"] = {
                    "data_fingerprint": data_fingerprint,
                    "report_ids": frozenset(),
                    "all_entries": None,
                    "settings_version": settings_version,
                    "identity_fp": identity_fp,
                    "resolution": None,
                    "resolved_entries": None,
                    "summary": None,
                    "settings": None,
                }
                g.current_data = result = None, None, None
                g.identity = None
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

            identity_changed = data_changed or not cache or cache.get("identity_fp") != identity_fp
            if identity_changed:
                codes, decisions = db.load_identity_inputs(conn)
                resolution = identity.resolve(all_entries, codes, decisions)
                resolved_entries = identity.apply(all_entries, resolution)
            else:
                resolution = cache["resolution"]
                resolved_entries = cache["resolved_entries"]

            if not identity_changed and cache["settings_version"] == settings_version:
                summary = cache["summary"]
                settings = cache["settings"]
            else:
                settings = db.get_settings(conn)
                summary = summarize_history(
                    resolved_entries,
                    trailing_days_target=settings["trailing_days_target"],
                    dead_stock_days=settings["dead_stock_days"],
                    low_stock_days=settings["low_stock_days"],
                    overstock_days=settings["overstock_days"],
                    value_tier_a_pct=settings["value_tier_a_pct"],
                    value_tier_b_pct=settings["value_tier_b_pct"],
                )

            self.app.config["_SUMMARY_CACHE"] = {
                "data_fingerprint": data_fingerprint,
                "report_ids": report_ids,
                "all_entries": all_entries,
                "settings_version": settings_version,
                "identity_fp": identity_fp,
                "resolution": resolution,
                "resolved_entries": resolved_entries,
                "summary": summary,
                "settings": settings,
            }
            result = resolved_entries, summary, settings
        g.current_data = result
        g.identity = resolution
        return result

    def current_identity(self) -> identity.Resolution | None:
        """The name grouping behind self.get_current_data()'s all_entries."""
        self.get_current_data()
        return g.identity

    def company_name(self) -> str | None:
        # Reuse the route's own self.get_current_data() call for free if it
        # already ran this request (Dashboard/Trends always call it) — but
        # don't trigger self.get_current_data() ourselves just to answer this.
        # Reports/Settings don't need a full summarize_history() over every
        # SKU (the expensive part, and the thing invalidated on every
        # upload/refresh/remove/settings-save — exactly the actions that
        # land back on these two pages) merely to print a company name in
        # the nav bar; list_reports() is ~180x cheaper for that alone.
        if "current_data" in g:
            _, summary, _ = g.current_data
            return summary.meta.company if summary else None
        with db.connect(self.app.config["DB_PATH"]) as conn:
            reports = db.list_reports(conn)
        if reports.empty:
            return None
        return reports.iloc[-1]["company"]
