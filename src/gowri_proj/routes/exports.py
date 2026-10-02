"""Excel/CSV downloads of the Dashboard's action lists and value segments."""

from __future__ import annotations

from flask import Flask, Response, jsonify, request

from ..dashboard import (
    SEGMENT_MOVEMENT_LABELS,
    SEGMENT_TIER_LABELS,
)
from ..datacache import DataCache
from ..excel_export import (
    action_lists_workbook,
    value_segment_columns,
    value_segment_rows,
    value_segments_workbook,
)
from ..web_helpers import (
    _csv_response,
)


def register(app: Flask, data: DataCache) -> None:
    get_current_data = data.get_current_data

    @app.get("/api/action-lists/export.xlsx")
    def api_action_lists_export_xlsx():
        """Every action list (out of stock, low, dead, overstock) in full,
        one sheet each, plus a Summary — the Dashboard's "Download all
        lists" button. Same workbook the CLI writes with --excel."""
        _, summary, _ = get_current_data()
        if summary is None:
            return jsonify(error="No reports imported yet — nothing to export."), 404
        filename = f"action_lists_{summary.meta.latest_period_end.isoformat()}.xlsx"
        return Response(
            action_lists_workbook(summary),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/value-segments/export.xlsx")
    def api_value_segments_export_xlsx():
        _, summary, thresholds = get_current_data()
        if summary is None:
            return jsonify(error="No reports imported yet — nothing to export."), 404
        content = value_segments_workbook(
            summary, thresholds["value_tier_a_pct"], thresholds["value_tier_b_pct"]
        )
        filename = f"value_segments_{summary.meta.latest_period_end.isoformat()}.xlsx"
        return Response(
            content,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/value-segments/export.csv")
    def api_value_segment_export_csv():
        # One tile's SKU list, same columns as that tile's sheet in the
        # .xlsx above. Server-side rather than the client-side exportCsv the
        # action lists use: the page's own value_segment_skus payload only
        # carries the four columns the tile table shows, not the extra ones
        # a download is for.
        tier = request.args.get("tier", "")
        movement = request.args.get("movement", "")
        if tier not in SEGMENT_TIER_LABELS or movement not in SEGMENT_MOVEMENT_LABELS:
            return jsonify(error="Unknown value segment."), 400
        _, summary, _ = get_current_data()
        if summary is None:
            return jsonify(error="No reports imported yet — nothing to export."), 404
        rows = value_segment_rows(summary, tier, movement)
        rows = rows.astype(object).where(rows.notna(), None)  # a missing brand is "", not "nan"
        filename = (
            f"value_segment_{tier}_{movement}_{summary.meta.latest_period_end.isoformat()}.csv"
        )
        return _csv_response(
            rows.to_dict(orient="records"),
            value_segment_columns(summary.meta.trailing_days),
            filename,
        )
