"""Scan a folder of stock statement exports and import whatever is new.

This is the "refresh" behind the app: drop each month's .xls export into a
folder — organized into subfolders however you like (e.g. one per financial
year) — and syncing re-scans it recursively. Unchanged files are skipped
without re-parsing (tracked by path-relative-to-the-folder + size + mtime),
new files are parsed and imported, a file whose period was already imported
under a different name is flagged rather than silently duplicated, and a
file that changed to cover a *different* period than it used to (same
identity, new period) is flagged too rather than quietly creating a second
report and orphaning the first.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import db
from .parser import parse_stock_statement

SUPPORTED_SUFFIXES = {".xls", ".xlsx"}


def fy_folder(d: date) -> str:
    """Indian financial-year folder name (Apr-Mar) for a given date, e.g.
    any date from Apr 2025 to Mar 2026 -> "2526". Matches the convention
    already used for every historical file sitting in uploads/ (organized
    by hand into one subfolder per FY) — used to auto-file new direct
    uploads (webapp.py's /api/upload) into the same structure instead of
    dropping them flat in the folder root.
    """
    fy_start_year = d.year if d.month >= 4 else d.year - 1
    return f"{fy_start_year % 100:02d}{(fy_start_year + 1) % 100:02d}"


def _is_candidate(path: Path, folder_path: Path) -> bool:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        return False
    if not path.is_file():
        return False
    rel_parts = path.relative_to(folder_path).parts
    # Excel lock files (~$...), dotfiles, and anything under a hidden folder.
    return not any(part.startswith("~$") or part.startswith(".") for part in rel_parts)


@dataclass
class SyncResult:
    imported: list[tuple[str, str, str, int]] = field(default_factory=list)  # rel_path, start, end, sku_count
    unchanged: list[str] = field(default_factory=list)
    duplicate_period: list[tuple[str, str, str]] = field(default_factory=list)  # rel_path, start, end
    filename_reused: list[tuple[str, str, str]] = field(default_factory=list)  # rel_path, old period, new period
    errors: list[tuple[str, str]] = field(default_factory=list)  # rel_path, error message


def sync_folder(conn: sqlite3.Connection, folder: str) -> SyncResult:
    result = SyncResult()
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return result

    candidates = sorted(
        (p for p in folder_path.rglob("*") if _is_candidate(p, folder_path)),
        key=lambda p: p.relative_to(folder_path).as_posix(),
    )

    for path in candidates:
        rel_name = path.relative_to(folder_path).as_posix()

        stat = path.stat()
        filesize, mtime = stat.st_size, int(stat.st_mtime)
        known = db.get_watched_file(conn, rel_name)
        if known is not None and known["filesize"] == filesize and known["mtime"] == mtime:
            result.unchanged.append(rel_name)
            continue

        try:
            df, meta = parse_stock_statement(str(path))
        except Exception as e:  # noqa: BLE001 — surfaced per-file, sync must not abort on one bad file
            result.errors.append((rel_name, str(e)))
            db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", str(e))
            continue

        if not meta.period_start or not meta.period_end:
            msg = "No reporting period found in the file banner"
            result.errors.append((rel_name, msg))
            db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", msg)
            continue

        if df.empty:
            msg = "No SKU rows could be read from this file — it may not be a stock statement export"
            result.errors.append((rel_name, msg))
            db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", msg)
            continue

        # If this file previously represented a *different* period, don't
        # silently create a second report while abandoning file-provenance for
        # the first — that leaves a stale report in the database whose
        # "source file" has actually been overwritten with something else.
        # This has to keep blocking on every retry (not just the first time
        # the mismatch is seen) — checking on report_id rather than the
        # watched_files status label means a repeated sync of the same
        # conflicting file can't quietly slip through on a second try. It
        # only stops blocking once the old report is actually gone (removed
        # via the Reports page / `remove`), at which point old_report is None.
        old_report = db.find_reused_filename_conflict(conn, known, meta)
        if old_report is not None:
            result.filename_reused.append(
                (
                    rel_name,
                    f"{old_report['period_start']} to {old_report['period_end']}",
                    f"{meta.period_start.isoformat()} to {meta.period_end.isoformat()}",
                )
            )
            db.upsert_watched_file(
                conn, rel_name, filesize, mtime, known["report_id"], "filename_reused"
            )
            continue

        existing_report_id = db.period_exists(conn, meta)
        if existing_report_id is not None and (known is None or known["report_id"] != existing_report_id):
            # This period is already in the database from a different file.
            result.duplicate_period.append(
                (rel_name, meta.period_start.isoformat(), meta.period_end.isoformat())
            )
            db.upsert_watched_file(
                conn, rel_name, filesize, mtime, existing_report_id, "duplicate_period"
            )
            continue

        try:
            import_result = db.import_report(
                conn, df, meta, rel_name, replace=(existing_report_id is not None)
            )
        except ValueError as e:  # a partial-overlap conflict — see find_superseded_reports
            result.errors.append((rel_name, str(e)))
            db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", str(e))
            continue
        db.upsert_watched_file(
            conn, rel_name, filesize, mtime, import_result.report_id, "imported"
        )
        result.imported.append(
            (rel_name, meta.period_start.isoformat(), meta.period_end.isoformat(), len(df))
        )

    return result
