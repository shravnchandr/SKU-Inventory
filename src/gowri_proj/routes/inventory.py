"""Looking things up: search, SKU and brand detail panels, month-by-month history."""

from __future__ import annotations

import threading

from flask import Flask, jsonify, request

from ..analysis import (
    brand_history,
    format_days_of_cover,
    search_skus,
    sku_history,
    status_history,
)
from ..dashboard import (
    _sanitize,
)
from ..datacache import DataCache
from ..web_helpers import (
    _value_segment_for,
)


def register(app: Flask, data: DataCache) -> None:
    get_current_data = data.get_current_data
    current_identity = data.current_identity

    # Serialises the first status_history() computation (see api_status_history).
    status_history_lock = threading.Lock()

    @app.get("/api/search")
    def api_search():
        q = request.args.get("q", "")
        _, summary, _ = get_current_data()
        if summary is None:
            return jsonify(results=[])
        # Also matches an item's earlier names (identity.py): an item renamed
        # since, e.g. tagged "(NON)", should still turn up when searched by
        # the name someone remembers.
        resolution = current_identity()
        results = search_skus(
            summary.enriched, q, aliases=resolution.aliases if resolution else None
        )
        return jsonify(results=results)

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
        resolution = current_identity()
        return jsonify(
            _sanitize(
                {
                    "brand": display_brand,
                    "sku": sku,
                    "current": current,
                    "value_segment": value_segment,
                    "history": history,
                    # Earlier names this item's history was joined from.
                    "aliases": resolution.aliases.get(sku, []) if resolution else [],
                }
            )
        )

    @app.get("/api/status-history")
    def api_status_history():
        """Month-by-month status breakdown and on-hand totals, for the
        dashboard's history popups. Expensive on a cold cache (one full
        summarize_history() per imported month — a few seconds), so it's
        computed once per data/settings state and only on request, never as
        part of rendering a page. The lock makes a second request that
        arrives mid-computation (the page's background pre-warm, then a
        click) wait for that result instead of starting its own.
        """
        all_entries, summary, thresholds = get_current_data()
        if summary is None:
            return jsonify(error="No data imported yet."), 404
        with status_history_lock:
            cache = app.config["_STATUS_HISTORY_CACHE"]
            if cache is not None and cache[0] is summary:
                points = cache[1]
            else:
                points = status_history(
                    all_entries,
                    trailing_days_target=thresholds["trailing_days_target"],
                    dead_stock_days=thresholds["dead_stock_days"],
                    low_stock_days=thresholds["low_stock_days"],
                    overstock_days=thresholds["overstock_days"],
                    value_tier_a_pct=thresholds["value_tier_a_pct"],
                    value_tier_b_pct=thresholds["value_tier_b_pct"],
                )
                app.config["_STATUS_HISTORY_CACHE"] = (summary, points)
        return jsonify(
            _sanitize(
                {
                    "points": points,
                    "trailing_days_target": thresholds["trailing_days_target"],
                    "dead_stock_days": thresholds["dead_stock_days"],
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
