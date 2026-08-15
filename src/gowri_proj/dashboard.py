"""Render the inventory analysis as a single self-contained HTML dashboard."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .analysis import STATUS_ORDER, InventorySummary, format_days_of_cover


def _sanitize(obj):
    """Recursively replace non-finite floats (inf/-inf/nan) with None.

    Python's json.dumps happily emits the bare tokens Infinity/-Infinity/NaN
    for these, which are not valid JSON and break JSON.parse in the browser.
    """
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj

STATUS_LABELS = {
    "out_of_stock": "Out of stock",
    "returned": "Returned",
    "dead_stock": "Dead stock",
    "low_stock": "Low stock",
    "overstock": "Overstock",
    "healthy": "Healthy",
}
STATUS_COLOR_VAR = {
    "out_of_stock": "--status-critical",
    "returned": "--status-neutral",
    "dead_stock": "--status-serious",
    "low_stock": "--status-warning",
    "overstock": "--series-1",
    "healthy": "--status-good",
}
def _status_blurbs(thresholds: dict) -> dict[str, str]:
    """Threshold-dependent copy — has to be built from whatever's actually
    configured, not hardcoded text, since low/overstock/dead-stock days are
    user-editable from the Settings page."""
    low = thresholds["low_stock_days"]
    over = thresholds["overstock_days"]
    dead = thresholds["dead_stock_days"]
    dead_desc = f"{dead} days" if dead != 90 else "3+ months"
    return {
        "out_of_stock": "Zero on hand — can't fulfil an order today.",
        "returned": "Zero on hand, nothing sold — stock was sent back, not sold through.",
        "low_stock": f"Selling well but under {low} days of cover left.",
        "dead_stock": f"Stock on hand, nothing sold since it was last purchased ({dead_desc}).",
        "overstock": f"Over {over} days of cover — capital tied up.",
        "healthy": "Comfortable stock relative to recent sales pace.",
    }


SEGMENT_TIER_LABELS = {
    "A": "A — top value",
    "B": "B — mid value",
    "C": "C — long tail",
}
SEGMENT_MOVEMENT_LABELS = {
    "fast": "Fast",
    "slow": "Slow",
    "non_moving": "Non-moving",
}
# One blurb per (tier, movement) combination — the actual "review/reorder
# policy" guidance the user asked for. No automation (this app has no
# ordering system): just a short recommendation shown next to each segment.
SEGMENT_POLICY = {
    ("A", "fast"): "Top priority — keep well stocked, review often.",
    ("A", "slow"): "High value moving slowly — review before reordering; consider a smaller order quantity.",
    ("A", "non_moving"): "High value tied up, not moving — review for return/markdown before it ages further.",
    ("B", "fast"): "Standard reorder cadence, no special attention needed.",
    ("B", "slow"): "Reduce reorder quantity; keep an eye on it.",
    ("B", "non_moving"): "Worth a look — candidate for markdown or return.",
    ("C", "fast"): "Low value, low risk — simplified reordering.",
    ("C", "slow"): "Low-value long tail — reorder only on request, minimal review.",
    ("C", "non_moving"): "Low-value dead stock — low priority, but a candidate to drop from the catalog.",
}


def _segment_policy(tier: str, movement: str) -> str:
    return SEGMENT_POLICY[(tier, movement)]


def _table_columns(trailing_days: int) -> list[tuple[str, str]]:
    """Action-list table columns.

    Opening/Purchased/Sold are labeled with the trailing window's length so
    it's clear they share one span (see analysis.summarize_history for why)
    — only Closing is a single point-in-time snapshot.
    """
    return [
        ("brand", "Brand"),
        ("sku", "SKU"),
        ("opening_stock", f"Opening ({trailing_days}d)"),
        ("purchase", f"Purchased ({trailing_days}d)"),
        ("sales", f"Sold ({trailing_days}d)"),
        ("closing_stock", "Closing (now)"),
        ("value", "Value (₹)"),
        ("days_of_cover", "Days cover"),
    ]


def _round_records(df: pd.DataFrame) -> list[dict]:
    if df.empty:
        return []
    d = df.copy()
    for col in ("opening_stock", "purchase", "sales", "closing_stock"):
        if col in d.columns:
            d[col] = d[col].round(0).astype(int)
    if "value" in d.columns:
        d["value"] = d["value"].round(2)
    if "days_of_cover" in d.columns:
        d["days_of_cover"] = d["days_of_cover"].apply(format_days_of_cover)
    return d.to_dict(orient="records")


DEFAULT_THRESHOLDS = {
    "low_stock_days": 15,
    "overstock_days": 90,
    "trailing_days_target": 90,
    "dead_stock_days": 90,
    "value_tier_a_pct": 70,
    "value_tier_b_pct": 90,
}


def build_payload(
    summary: InventorySummary,
    quality_issues: list[dict] | None = None,
    thresholds: dict | None = None,
    include_tables: bool = True,
) -> dict:
    """Build the full page payload.

    ``include_tables=False`` skips the row-level SKU tables (out_of_stock/
    low_stock/dead_stock/overstock — the biggest thing in this payload by
    far, ~1.36MB of the ~1.37MB total on a real 15k-SKU database) and the
    table-column definitions that only exist to label them. Pass this from
    any page that doesn't render those tables — e.g. the Trends page, which
    only reads meta/kpis/leaderboards/trend and would otherwise silently
    ship the entire action-list dataset a second time on every visit for no
    reason. The standalone CLI export (dashboard_template.html) needs both
    on one page, so it keeps the default.
    """
    meta = summary.meta
    thresholds = thresholds or DEFAULT_THRESHOLDS
    blurbs = _status_blurbs(thresholds)
    status_rows = []
    for status in STATUS_ORDER:
        status_rows.append(
            {
                "status": status,
                "label": STATUS_LABELS[status],
                "blurb": blurbs[status],
                "count": int(summary.status_counts.get(status, 0)),
                "value": round(float(summary.status_values.get(status, 0.0)), 2),
                "color_var": STATUS_COLOR_VAR[status],
            }
        )

    tables = {}
    table_columns: list[tuple[str, str]] = []
    dead_stock_table_columns: list[tuple[str, str]] = []
    if include_tables:
        # "returned" is intentionally not a table here — it's aggregate-only
        # (status_breakdown's count/value), grouped with "healthy" as
        # information rather than an actionable, per-SKU list. See
        # STATUS_ORDER's comment in analysis.py for why.
        tables = {
            "out_of_stock": _round_records(summary.out_of_stock),
            "low_stock": _round_records(summary.low_stock),
            "dead_stock": _round_records(summary.dead_stock),
            "overstock": _round_records(summary.overstock),
        }
        table_columns = _table_columns(meta.trailing_days)
        dead_stock_table_columns = table_columns + [("days_since_activity", "Days dead")]

    value_segments = [
        {
            **s,
            "tier_label": SEGMENT_TIER_LABELS[s["tier"]],
            "movement_label": SEGMENT_MOVEMENT_LABELS[s["movement"]],
            "policy": _segment_policy(s["tier"], s["movement"]),
        }
        for s in summary.value_segments
    ]
    value_segment_skus = _round_records(summary.value_segment_skus) if include_tables else []

    trend = summary.trend

    return {
        "meta": {
            "company": meta.company,
            "location": meta.location,
            "earliest_period_start": meta.earliest_period_start.isoformat(),
            "latest_period_end": meta.latest_period_end.isoformat(),
            "report_count": meta.report_count,
            "trailing_days": meta.trailing_days,
            "trailing_window_gaps": meta.trailing_window_gaps,
            "thresholds": thresholds,
        },
        "kpis": {
            "total_skus": summary.total_skus,
            "total_brands": summary.total_brands,
            "total_value": round(summary.total_value, 2),
            "total_units": round(summary.total_units, 0),
        },
        "status_breakdown": status_rows,
        "top_brands_by_value": [
            {"label": r["brand"], "brand": r["brand"], "value": round(r["value"], 2)}
            for r in summary.top_brands_by_value.to_dict(orient="records")
        ],
        "top_skus_by_value": [
            {
                "label": f"{r['sku']} ({r['brand']})",
                "brand": r["brand"],
                "sku": r["sku"],
                "value": round(r["value"], 2),
            }
            for r in summary.top_skus_by_value.to_dict(orient="records")
        ],
        "top_skus_by_sales": [
            {
                "label": f"{r['sku']} ({r['brand']})",
                "brand": r["brand"],
                "sku": r["sku"],
                "value": round(r["sales"], 0),
            }
            for r in summary.top_skus_by_sales.to_dict(orient="records")
        ],
        "tables": tables,
        "table_columns": table_columns,
        "dead_stock_table_columns": dead_stock_table_columns,
        "dead_stock_aging": summary.dead_stock_aging if include_tables else [],
        "value_segments": value_segments,
        "value_segment_skus": value_segment_skus,
        "trend": {
            "labels": trend.labels,
            "period_ends": trend.period_ends,
            "inventory_value": trend.inventory_value,
            "units_sold": trend.units_sold,
            "top_brands": trend.top_brands,
            "brand_units_sold": trend.brand_units_sold,
        },
        "quality_issues": quality_issues or [],
    }


def render_html(
    summary: InventorySummary,
    quality_issues: list[dict] | None = None,
    thresholds: dict | None = None,
) -> str:
    payload = _sanitize(build_payload(summary, quality_issues, thresholds))
    # This file is built by plain string substitution, not Jinja, so there's
    # no `tojson` filter doing this for us — escape the characters that
    # matter for breaking out of a `<script>` tag by hand (the same escaping
    # Flask's tojson applies). Brand/SKU/filename text in the payload comes
    # straight from an uploaded spreadsheet, so this isn't just theoretical.
    data_json = (
        json.dumps(payload)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    company = payload["meta"]["company"] or "Inventory"
    template = Path(__file__).with_name("dashboard_template.html").read_text()
    return template.replace("__TITLE__", f"{company} — Inventory Dashboard").replace(
        "__DATA_JSON__", data_json
    )


def write_dashboard(
    summary: InventorySummary,
    out_path: str,
    quality_issues: list[dict] | None = None,
    thresholds: dict | None = None,
) -> Path:
    html = render_html(summary, quality_issues, thresholds)
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html)
    return path
