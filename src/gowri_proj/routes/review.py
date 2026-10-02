"""The renamed-items review (identity.py): listing, deciding, undoing, exporting."""

from __future__ import annotations

import pandas as pd
from flask import Flask, jsonify, request

from .. import db, identity
from ..dashboard import (
    _sanitize,
)
from ..datacache import DataCache
from ..web_helpers import (
    _csv_response,
)


def register(app: Flask, data: DataCache) -> None:
    get_current_data = data.get_current_data
    current_identity = data.current_identity

    # ---------- renamed items (identity.py) ----------

    def _latest_brand_by_sku(entries: pd.DataFrame) -> dict[str, str]:
        """Each item's brand as of the most recent report it's in — not just
        the latest report, which a merged-then-discontinued item isn't in."""
        latest = entries.sort_values(["period_end", "period_start"]).drop_duplicates(
            "sku", keep="last"
        )
        return latest.set_index("sku")["brand"].to_dict()

    @app.get("/api/sku-merges")
    def api_sku_merges():
        """Everything the Reports page's "Renamed items" card shows: merges
        already applied (automatic and approved), suggestions waiting for a
        decision, and every stored decision (so any of them can be undone)."""
        all_entries, summary, _ = get_current_data()
        resolution = current_identity()
        if summary is None or resolution is None:
            return jsonify(merged=[], pending=[], decided=[])
        current_brand = summary.enriched.set_index("sku")["brand"].to_dict()
        brand_of = _latest_brand_by_sku(all_entries)
        merged = [
            {
                **m,
                "brand": brand_of.get(m["display_name"]),
                "current": m["display_name"] in current_brand,
            }
            for m in resolution.merges
        ]
        return jsonify(
            _sanitize(
                {
                    "merged": merged,
                    "pending": resolution.suggestions,
                    "decided": list(reversed(resolution.decided)),  # newest first
                }
            )
        )

    def _merge_pair_from_request() -> tuple[str, str] | tuple[None, None]:
        body = request.get_json(silent=True) or {}
        old, new = body.get("old_name"), body.get("new_name")
        if not (isinstance(old, str) and isinstance(new, str)) or not old or not new or old == new:
            return None, None
        return old, new

    @app.post("/api/sku-merges")
    def api_sku_merge_decide():
        """Record a person's decision on one pair of names: "merge" (same
        item) or "separate" (different items — rejects a suggestion, or
        splits an automatic merge)."""
        old, new = _merge_pair_from_request()
        decision = (request.get_json(silent=True) or {}).get("decision")
        if old is None or decision not in (identity.DECISION_MERGE, identity.DECISION_SEPARATE):
            return jsonify(
                error="Expected old_name, new_name and a decision of 'merge' or 'separate'."
            ), 400
        resolution = current_identity()
        if (
            resolution is None
            or old not in resolution.display_name
            or new not in resolution.display_name
        ):
            return jsonify(error="Those names aren't in any imported report."), 404
        with db.connect(app.config["DB_PATH"]) as conn:
            db.set_merge_decision(conn, old, new, decision)
        return jsonify(status="saved", old_name=old, new_name=new, decision=decision)

    @app.post("/api/sku-merges/undo")
    def api_sku_merge_undo():
        """Forget a stored decision: the pair goes back to what the
        automatic rules say."""
        old, new = _merge_pair_from_request()
        if old is None:
            return jsonify(error="Expected old_name and new_name."), 400
        with db.connect(app.config["DB_PATH"]) as conn:
            removed = db.clear_merge_decision(conn, old, new)
        return jsonify(status="undone" if removed else "nothing to undo")

    @app.get("/api/sku-merges/export.csv")
    def api_sku_merges_export():
        """Every applied merge and every pending suggestion, for checking
        the whole list in Excel."""
        all_entries, summary, _ = get_current_data()
        resolution = current_identity()
        rows = []
        if resolution is not None and summary is not None:
            brand_of = _latest_brand_by_sku(all_entries)
            rows += [
                {
                    "status": "Merged",
                    "how": m["reason"],
                    "brand": brand_of.get(m["display_name"]),
                    **m,
                }
                for m in resolution.merges
            ]
            rows += [
                {"status": "Waiting for review", "how": "stock carried over", **sug}
                for sug in resolution.suggestions
            ]
        return _csv_response(
            rows,
            [
                ("status", "Status"),
                ("old_name", "Name"),
                ("new_name", "Same item as"),
                ("how", "How recognised"),
                ("display_name", "Shown as"),
                ("brand", "Brand"),
                ("period_end", "Renamed in report ending"),
                ("stock", "Stock carried over"),
            ],
            "renamed_items.csv",
        )

    @app.get("/api/review-count")
    def api_review_count():
        """How many renamed items are waiting for a decision — for the
        badge on the top bar's Review tab, on every page."""
        _, summary, _ = get_current_data()
        resolution = current_identity()
        return jsonify(
            pending=len(resolution.suggestions) if summary is not None and resolution else 0
        )
