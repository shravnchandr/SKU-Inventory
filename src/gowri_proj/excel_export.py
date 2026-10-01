"""Export each action list (and the overall summary) to a multi-sheet .xlsx workbook,
and the value segments (ABC x movement) to their own workbook/CSV for download."""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd

from .analysis import SEGMENT_ORDER, STATUS_ORDER, InventorySummary, format_days_of_cover
from .dashboard import SEGMENT_MOVEMENT_LABELS, SEGMENT_TIER_LABELS, _segment_policy


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
                length = max(
                    (len(str(c.value)) for c in col_cells if c.value is not None), default=10
                )
                ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 40)

    return path


def value_segment_columns(trailing_days: int) -> list[tuple[str, str]]:
    """(key, label) columns for one value segment's SKU list — shared by the
    per-tile CSV and each segment sheet of the workbook, so the two
    downloads never disagree about what a column means.

    Wider than the dashboard tile's own table (Brand/SKU/Value/Days cover):
    a download is for working through a list offline — e.g. deciding which
    long-tail non-moving SKUs to drop — where closing stock, units sold and
    how long since anything happened are what that decision turns on.
    """
    return [
        ("brand", "Brand"),
        ("sku", "SKU"),
        ("closing_stock", "Closing Stock (now)"),
        ("value", "Value (Rs)"),
        ("sales", f"Sold ({trailing_days}d)"),
        ("days_of_cover", "Days of Cover"),
        ("days_since_activity", "Days Since Activity"),
    ]


def value_segment_rows(summary: InventorySummary, tier: str, movement: str) -> pd.DataFrame:
    """One segment's SKUs, highest value first, with the extra columns from
    ``value_segment_columns`` joined in from ``summary.enriched`` (keyed on
    sku — same key value_segment_skus was built on). Days of cover is None,
    not inf, when a SKU has no demand — inf would print as "inf" in a CSV
    and can't be written to an Excel cell at all.

    Numbers are rounded the way a person would write them down: value to
    paise, days of cover to one decimal (same as the dashboard), and unit
    counts to whole numbers when they are whole ("10", not "10.0" — the
    source export's quantities are floats, but almost always integral).
    """
    keys = [k for k, _ in value_segment_columns(summary.meta.trailing_days)]
    segs = summary.value_segment_skus
    rows = segs[(segs["tier"] == tier) & (segs["movement"] == movement)][["sku"]]
    extra = summary.enriched[keys].drop_duplicates("sku")
    rows = rows.merge(extra, on="sku", how="left")[keys]
    rows = rows.sort_values("value", ascending=False).reset_index(drop=True)
    rows["value"] = rows["value"].round(2)
    # dtype=object, or pandas would coerce a mixed int/None/float column
    # straight back to float64 — undoing exactly what these conversions do.
    rows["days_of_cover"] = pd.Series(
        [format_days_of_cover(v) for v in rows["days_of_cover"]], index=rows.index, dtype=object
    )
    for col in ("closing_stock", "sales"):
        rows[col] = pd.Series(
            [_whole_if_integral(v) for v in rows[col]], index=rows.index, dtype=object
        )
    return rows


def _whole_if_integral(value: float) -> int | float:
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def value_segment_sheet_name(tier: str, movement: str) -> str:
    """e.g. "A - Non-moving". Well under Excel's 31-char sheet-name limit,
    and free of the characters it forbids (/ \\ ? * [ ] :)."""
    return f"{tier} - {SEGMENT_MOVEMENT_LABELS[movement]}"


def value_segments_workbook(summary: InventorySummary, a_pct: float, b_pct: float) -> bytes:
    """The whole value-segments card as one .xlsx: a Summary sheet with the
    9 tiles (count, value, recommendation), then one sheet per segment
    listing its SKUs.

    Built in memory and returned as bytes rather than written to disk —
    a temp file is exactly what Windows antivirus likes to lock mid-write
    (see webapp._replace_with_retry), and there's nothing to clean up.
    """
    columns = value_segment_columns(summary.meta.trailing_days)
    labels = dict(columns)
    counts = {(s["tier"], s["movement"]): s for s in summary.value_segments}

    summary_df = pd.DataFrame(
        [
            {
                "Tier": SEGMENT_TIER_LABELS[tier],
                "Movement": SEGMENT_MOVEMENT_LABELS[movement],
                "SKUs": counts[(tier, movement)]["count"],
                "Value (Rs)": counts[(tier, movement)]["value"],
                "Recommendation": _segment_policy(tier, movement),
                "Sheet": value_segment_sheet_name(tier, movement),
            }
            for tier, movement in SEGMENT_ORDER
        ]
    )
    notes = [
        f"Value segments as of {summary.meta.latest_period_end.isoformat()}"
        + (f" — {summary.meta.company}" if summary.meta.company else ""),
        (
            f"Tier A = the top {a_pct:g}% of stock value, B up to {b_pct:g}%, C the long tail. "
            "Movement: Fast = selling normally, Slow = overstocked, Non-moving = dead stock. "
            "Out-of-stock and returned SKUs aren't segmented."
        ),
    ]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False, startrow=len(notes) + 1)
        ws = writer.sheets["Summary"]
        for i, note in enumerate(notes, start=1):
            ws.cell(row=i, column=1, value=note)

        for tier, movement in SEGMENT_ORDER:
            rows = value_segment_rows(summary, tier, movement).rename(columns=labels)
            rows.to_excel(writer, sheet_name=value_segment_sheet_name(tier, movement), index=False)

        for ws in writer.sheets.values():
            # Auto-fit (approximate) like export_excel above — but skip the
            # Summary notes rows, or one long sentence would stretch column A.
            header_row = len(notes) + 2 if ws.title == "Summary" else 1
            for col_cells in ws.iter_cols(min_row=header_row):
                length = max(
                    (len(str(c.value)) for c in col_cells if c.value is not None), default=10
                )
                ws.column_dimensions[col_cells[0].column_letter].width = min(length + 2, 60)
                if col_cells[0].value == "Value (Rs)":
                    for c in col_cells[1:]:
                        c.number_format = "#,##0.00"
            ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    return buf.getvalue()
