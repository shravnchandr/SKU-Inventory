"""The Settings page and saving it."""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

from .. import db
from ..analysis import (
    THRESHOLD_DAYS_MAX,
    THRESHOLD_DAYS_MIN,
    THRESHOLD_PCT_MAX,
    THRESHOLD_PCT_MIN,
)
from ..datacache import DataCache


def register(app: Flask, data: DataCache) -> None:
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
        # Low stock is checked before overstock, so a low-stock threshold at
        # or above the overstock one would silently leave nothing that could
        # ever be "healthy" between them.
        if values["low_stock_days"] >= values["overstock_days"]:
            return jsonify(
                error=(
                    f"Low stock ({values['low_stock_days']} days) must be fewer days of cover than "
                    f"overstock ({values['overstock_days']} days)."
                )
            ), 400
        if values["value_tier_a_pct"] >= values["value_tier_b_pct"]:
            return jsonify(error="'value_tier_a_pct' must be less than 'value_tier_b_pct'."), 400
        with db.connect(app.config["DB_PATH"]) as conn:
            saved = db.upsert_settings(conn, **values)
        return jsonify(saved)
