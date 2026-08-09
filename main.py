"""CLI for the inventory app.

Everyday workflow:
    uv run main.py refresh --open      # scan uploads/, import anything new, rebuild the dashboard

Drop each month's stock statement export into uploads/ (default folder) and
run `refresh` — it only imports files it hasn't seen before, so it's safe to
run any time. `import` remains for loading a specific file from elsewhere.

    uv run main.py import stocknsales.xls   # load one specific file into the database
    uv run main.py dashboard                # rebuild the HTML dashboard without touching uploads/
    uv run main.py list                     # see what's been imported

Every import is one period (ideally one calendar month, going forward). The
dashboard always reflects the full history in the database: current stock
from the latest import, sales trends across all imports.
"""
from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from src.gowri_proj import db
from src.gowri_proj.analysis import find_data_quality_issues, find_import_gaps, summarize_history
from src.gowri_proj.dashboard import write_dashboard
from src.gowri_proj.excel_export import export_excel
from src.gowri_proj.parser import parse_stock_statement
from src.gowri_proj.sync import SyncResult, sync_folder

DEFAULT_UPLOADS_DIR = "uploads"


def cmd_import(args: argparse.Namespace) -> None:
    print(f"Reading {args.xls_path} ...")
    df, meta = parse_stock_statement(args.xls_path)
    with db.connect(args.db) as conn:
        result = db.import_report(conn, df, meta, Path(args.xls_path).name, replace=args.replace)
    verb = "Replaced" if result.replaced else "Imported"
    print(
        f"{verb} report for {result.period_start} to {result.period_end}: "
        f"{result.sku_count:,} SKUs (report id {result.report_id})"
    )
    print("Run `uv run main.py dashboard` to rebuild the dashboard with this data.")


def cmd_list(args: argparse.Namespace) -> None:
    with db.connect(args.db) as conn:
        reports = db.list_reports(conn)
    if reports.empty:
        print("No reports imported yet. Run `uv run main.py refresh` first.")
        return
    for _, r in reports.iterrows():
        print(
            f"[{r['id']:>3}] {r['period_start'].date()} to {r['period_end'].date()} "
            f"({r['period_days']}d)  {r['sku_count']:>6,} SKUs  "
            f"from {r['source_filename']}  imported {r['imported_at']}"
        )

    gaps = find_import_gaps(reports)
    if gaps["missing_months"] or gaps["coverage_gaps"]:
        print()
        if gaps["missing_months"]:
            print(f"⚠ No report at all covers: {', '.join(gaps['missing_months'])}")
        for g in gaps["coverage_gaps"]:
            print(f"⚠ {g['days']}-day gap between reports: {g['gap_start']} to {g['gap_end']}")


def cmd_remove(args: argparse.Namespace) -> None:
    with db.connect(args.db) as conn:
        report = db.get_report(conn, args.report_id)
        if report is None:
            print(f"No report with id {args.report_id}. Run `uv run main.py list` to see valid ids.")
            return
        print(
            f"Removing report [{report['id']}] {report['period_start']} to {report['period_end']} "
            f"({report['sku_count']:,} SKUs, from {report['source_filename']})"
        )
        db.delete_report(conn, args.report_id)
    print(
        "Removed. If the source file is still in the uploads folder, the next "
        "`refresh` will treat it as new — fix it first or delete/move it if you "
        "don't want it re-imported. Run `dashboard` (or `refresh`) to rebuild with the change."
    )


def _report_sync(result: SyncResult) -> None:
    for filename, start, end, sku_count in result.imported:
        print(f"  + imported {filename}: {start} to {end} ({sku_count:,} SKUs)")
    for filename, start, end in result.duplicate_period:
        print(
            f"  ! skipped {filename}: period {start} to {end} is already imported "
            f"under a different filename"
        )
    for filename, old_period, new_period in result.filename_reused:
        print(
            f"  ! skipped {filename}: it previously represented {old_period}; this version is for "
            f"{new_period}. That old report is still in the database — remove it (see `list`/`remove`) "
            f"first if this new file is correct, then refresh again."
        )
    for filename, error in result.errors:
        print(f"  ✗ skipped {filename}: {error}")
    if result.unchanged:
        print(f"  = {len(result.unchanged)} file(s) unchanged since last refresh")
    if not (result.imported or result.duplicate_period or result.filename_reused or result.errors or result.unchanged):
        print(f"  (no files found)")


def cmd_refresh(args: argparse.Namespace) -> None:
    print(f"Scanning {args.folder}/ ...")
    with db.connect(args.db) as conn:
        result = sync_folder(conn, args.folder)
        _report_sync(result)
        if not db.has_data(conn):
            print(f"\nNo data in the database yet — add .xls/.xlsx exports to {args.folder}/ and refresh again.")
            return
    print()
    _build_dashboard(args)


def cmd_dashboard(args: argparse.Namespace) -> None:
    _build_dashboard(args)


def _resolve_thresholds(conn, args: argparse.Namespace) -> dict:
    """Persisted Settings values, with any explicitly-passed CLI flag as a
    one-off override for this run only (not written back to the DB) — so the
    CLI and the web app's Settings page share one source of truth by default.
    """
    settings = db.get_settings(conn)
    overrides = {
        "low_stock_days": args.low_stock_days,
        "overstock_days": args.overstock_days,
        "trailing_days_target": args.trailing_days,
        "dead_stock_days": args.dead_stock_days,
    }
    for key, value in overrides.items():
        if value is not None:
            settings[key] = value
    return settings


def _build_dashboard(args: argparse.Namespace) -> None:
    with db.connect(args.db) as conn:
        if not db.has_data(conn):
            print("No reports imported yet. Run `uv run main.py refresh` first.")
            return
        all_entries = db.load_all_entries(conn)
        thresholds = _resolve_thresholds(conn, args)

    summary = summarize_history(
        all_entries,
        trailing_days_target=thresholds["trailing_days_target"],
        dead_stock_days=thresholds["dead_stock_days"],
        low_stock_days=thresholds["low_stock_days"],
        overstock_days=thresholds["overstock_days"],
    )
    meta = summary.meta

    print(f"{meta.company or 'Inventory'} ({meta.location or '-'})")
    print(
        f"History: {meta.earliest_period_start} to {meta.latest_period_end} "
        f"across {meta.report_count} imported report(s)"
    )
    print(f"Sales pace based on the trailing {meta.trailing_days} days")
    print(f"SKUs: {summary.total_skus:,}  Brands: {summary.total_brands:,}")
    print(f"Inventory value on hand: Rs {summary.total_value:,.0f}")
    print(f"Units on hand: {summary.total_units:,.0f}\n")

    print("Stock health:")
    for status in ["out_of_stock", "low_stock", "dead_stock", "overstock", "healthy"]:
        count = summary.status_counts.get(status, 0)
        value = summary.status_values.get(status, 0.0)
        print(f"  {status:14s} {count:6,d} SKUs   Rs {value:>14,.0f}")

    issues = find_data_quality_issues(all_entries)
    if issues:
        print(
            f"\n⚠ {len(issues)} data quality issue(s) found in the source files (impossible values, "
            f"e.g. positive stock with negative value) — see the dashboard's Data Quality card, or:"
        )
        for i in issues[:5]:
            print(f"  [{i['period_label']}] {i['brand']} / {i['sku']}: {i['issue']}")
        if len(issues) > 5:
            print(f"  ...and {len(issues) - 5} more")

    out_path = write_dashboard(summary, args.out, issues, thresholds)
    print(f"\nDashboard written to {out_path.resolve()}")

    if args.excel:
        excel_path = export_excel(summary, args.excel)
        print(f"Excel workbook written to {excel_path.resolve()}")

    if args.open:
        webbrowser.open(f"file://{out_path.resolve()}")


def _add_dashboard_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--out", default="output/inventory_dashboard.html", help="Output HTML path")
    p.add_argument("--open", action="store_true", help="Open the dashboard in a browser when done")
    p.add_argument(
        "--excel",
        nargs="?",
        const="output/inventory_lists.xlsx",
        default=None,
        metavar="PATH",
        help="Also export each action list to a multi-sheet Excel workbook "
        "(default: output/inventory_lists.xlsx)",
    )
    p.add_argument(
        "--trailing-days",
        type=int,
        default=None,
        help="Minimum days of recent history to base the sales pace on "
        "(default: whatever's saved in Settings, or 90 if nothing's been saved)",
    )
    p.add_argument(
        "--dead-stock-days",
        type=int,
        default=None,
        help="A SKU is dead stock once this many days have passed with no sales since its "
        "last purchase (default: whatever's saved in Settings, or 90 if nothing's been saved)",
    )
    p.add_argument(
        "--low-stock-days",
        type=int,
        default=None,
        help="A SKU is low stock once its days-of-cover drops below this "
        "(default: whatever's saved in Settings, or 15 if nothing's been saved)",
    )
    p.add_argument(
        "--overstock-days",
        type=int,
        default=None,
        help="A SKU is overstocked once its days-of-cover exceeds this "
        "(default: whatever's saved in Settings, or 90 if nothing's been saved)",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Track pharmacy inventory across monthly stock statement exports.")
    ap.add_argument("--db", default=db.DEFAULT_DB_PATH, help="SQLite database path")
    sub = ap.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser(
        "refresh", help="Scan the uploads folder for new/changed files, import them, and rebuild the dashboard"
    )
    p_refresh.add_argument(
        "--folder", default=DEFAULT_UPLOADS_DIR, help=f"Folder to scan (default: {DEFAULT_UPLOADS_DIR})"
    )
    _add_dashboard_args(p_refresh)
    p_refresh.set_defaults(func=cmd_refresh)

    p_import = sub.add_parser("import", help="Load one specific stock statement .xls into the database")
    p_import.add_argument("xls_path", help="Path to the .xls export for one period (ideally one month)")
    p_import.add_argument(
        "--replace", action="store_true", help="Overwrite an existing import for the same period"
    )
    p_import.set_defaults(func=cmd_import)

    p_list = sub.add_parser("list", help="List every report imported so far")
    p_list.set_defaults(func=cmd_list)

    p_remove = sub.add_parser(
        "remove", help="Delete an imported report (wrong file, duplicate month, etc.) — see `list` for ids"
    )
    p_remove.add_argument("report_id", type=int, help="Report id from `uv run main.py list`")
    p_remove.set_defaults(func=cmd_remove)

    p_dash = sub.add_parser("dashboard", help="Rebuild the HTML dashboard from all imported data")
    _add_dashboard_args(p_dash)
    p_dash.set_defaults(func=cmd_dashboard)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
