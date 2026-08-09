"""Export each action list (and the overall summary) to a multi-sheet .xlsx workbook."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .analysis import STATUS_ORDER, InventorySummary

def _display_columns(trailing_days: int) -> dict[str, str]:
    """Column labels for the exported sheets — same trailing-window
    labeling as dashboard._table_columns (see analysis.summarize_history
    for why Opening/Purchase/Sold share one window and Closing doesn't).
    """
    return {
        "brand": "Brand",
        "sku": "SKU",
        "opening_stock": f"Opening Stock ({trailing_days}d)",
        "purchase": f"Purchase ({trailing_days}d)",
        "sales": f"Sold ({trailing_days}d)",
        "closing_stock": "Closing Stock (now)",
        "value": "Value (Rs)",
        "days_of_cover": "Days of Cover",
        "days_since_activity": "Days Since Activity",
    }


# "returned" deliberately has no sheet — same call as the dashboard's
# static, non-clickable row: aggregate count/value only (in the Summary
# sheet below via STATUS_ORDER), no per-SKU list.
SHEETS = [
    ("out_of_stock", "Out of Stock"),
    ("low_stock", "Low Stock"),
    ("dead_stock", "Dead Stock"),
    ("overstock", "Overstock"),
]


def _prep(df: pd.DataFrame, display_columns: dict[str, str]) -> pd.DataFrame:
    d = df.rename(columns=display_columns).copy()
    if "Days of Cover" in d.columns:
        d["Days of Cover"] = d["Days of Cover"].replace(float("inf"), None)
    return d


def _summary_frame(summary: InventorySummary) -> pd.DataFrame:
    labels = {
        "out_of_stock": "Out of Stock",
        "returned": "Returned",
        "low_stock": "Low Stock",
        "dead_stock": "Dead Stock",
        "overstock": "Overstock",
        "healthy": "Healthy",
    }
    rows = [
        {
            "Status": labels[s],
            "SKUs": summary.status_counts.get(s, 0),
            "Value (Rs)": round(summary.status_values.get(s, 0.0), 2),
        }
        for s in STATUS_ORDER
    ]
    rows.append(
        {
            "Status": "Total",
            "SKUs": summary.total_skus,
            "Value (Rs)": round(summary.total_value, 2),
        }
    )
    return pd.DataFrame(rows)


def export_excel(summary: InventorySummary, out_path: str) -> Path:
    """Write one workbook with a sheet per action list, plus a Summary sheet."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    display_columns = _display_columns(summary.meta.trailing_days)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        _summary_frame(summary).to_excel(writer, sheet_name="Summary", index=False)

        lists = {
            "out_of_stock": summary.out_of_stock,
            "low_stock": summary.low_stock,
            "dead_stock": summary.dead_stock,
            "overstock": summary.overstock,
        }
        for key, sheet_name in SHEETS:
            _prep(lists[key], display_columns).to_excel(writer, sheet_name=sheet_name, index=False)

        # Auto-fit column widths (approximate, based on header + longest value).
        for sheet_name in ["Summary"] + [name for _, name in SHEETS]:
            ws = writer.sheets[sheet_name]
            for col_cells in ws.columns:
                length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=10)
                ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 40)

    return path
