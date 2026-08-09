"""Render the inventory analysis as a single self-contained HTML dashboard."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .analysis import InventorySummary


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
    "dead_stock": "Dead stock",
    "low_stock": "Low stock",
    "overstock": "Overstock",
    "healthy": "Healthy",
}
STATUS_ORDER = ["out_of_stock", "low_stock", "dead_stock", "overstock", "healthy"]
STATUS_COLOR_VAR = {
    "out_of_stock": "--status-critical",
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
        "low_stock": f"Selling well but under {low} days of cover left.",
        "dead_stock": f"Stock on hand, nothing sold since it was last purchased ({dead_desc}).",
        "overstock": f"Over {over} days of cover — capital tied up.",
        "healthy": "Comfortable stock relative to recent sales pace.",
    }


TABLE_COLUMNS = [
    ("brand", "Brand"),
    ("sku", "SKU"),
    ("opening_stock", "Opening"),
    ("purchase", "Purchased"),
    ("sales", "Sold"),
    ("closing_stock", "Closing"),
    ("value", "Value (₹)"),
    ("days_of_cover", "Days cover"),
]

DEAD_STOCK_TABLE_COLUMNS = TABLE_COLUMNS + [("days_since_activity", "Days dead")]


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
        d["days_of_cover"] = d["days_of_cover"].apply(
            lambda v: None if v == float("inf") else round(v, 1)
        )
    return d.to_dict(orient="records")


DEFAULT_THRESHOLDS = {
    "low_stock_days": 15,
    "overstock_days": 90,
    "trailing_days_target": 90,
    "dead_stock_days": 90,
}


def build_payload(
    summary: InventorySummary,
    quality_issues: list[dict] | None = None,
    thresholds: dict | None = None,
) -> dict:
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

    tables = {
        "out_of_stock": _round_records(summary.out_of_stock),
        "low_stock": _round_records(summary.low_stock),
        "dead_stock": _round_records(summary.dead_stock),
        "overstock": _round_records(summary.overstock),
    }

    trend = summary.trend

    return {
        "meta": {
            "company": meta.company,
            "location": meta.location,
            "earliest_period_start": meta.earliest_period_start.isoformat(),
            "latest_period_end": meta.latest_period_end.isoformat(),
            "report_count": meta.report_count,
            "trailing_days": meta.trailing_days,
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
        "table_columns": TABLE_COLUMNS,
        "dead_stock_table_columns": DEAD_STOCK_TABLE_COLUMNS,
        "dead_stock_aging": summary.dead_stock_aging,
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
