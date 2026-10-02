"""Scan a folder of stock statement exports and import whatever is new.

This is the "refresh" behind the app: drop each month's .xls export into a
folder — organized into subfolders however you like (e.g. one per financial
year) — and syncing re-scans it recursively. Unchanged files are skipped
without re-parsing (tracked by path-relative-to-the-folder + size + mtime),
new files are parsed and imported, a file whose period was already imported
under a different name is flagged rather than silently duplicated, and a
file that changed to cover a *different* period than it used to (same
identity, new period) is flagged too rather than quietly creating a second
report and orphaning the first. When two files' periods overlap, the one
saved most recently wins (by file modification time); the older one is
recorded as superseded and left alone on later scans.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from . import db
from .parser import is_item_list, parse_item_list, parse_stock_statement

SUPPORTED_SUFFIXES = {".xls", ".xlsx"}
DEFAULT_UPLOADS_DIR = "uploads"


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
    return not any(part.startswith(("~$", ".")) for part in rel_parts)


@dataclass
class SyncResult:
    imported: list[tuple[str, str, str, int]] = field(
        default_factory=list
    )  # rel_path, start, end, sku_count
    unchanged: list[str] = field(default_factory=list)
    duplicate_period: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # rel_path, start, end
    filename_reused: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # rel_path, old period, new period
    superseded: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # rel_path, its own period, newer file that covers it
    item_lists: list[str] = field(default_factory=list)  # rel_path of item list files seen
    item_lists_imported: list[tuple[str, str]] = field(
        default_factory=list
    )  # rel_path, list date — a newer item list found in the folder and imported
    errors: list[tuple[str, str]] = field(default_factory=list)  # rel_path, error message


ITEM_LIST_STATUS = "item_list"

# The current item list, always at this path in uploads/ (the Reports page
# saves it here; a rescan that imports a newer one copies it here too).
ITEM_CATALOG_FILENAME = "item_catalog.xlsx"

# Every item list imported is also kept, dated, under
# uploads/item_lists/<FY>/item_list_<date>.<ext> — but only the last six
# months of them: the rename log in the database already remembers every
# rename an item list revealed, so older files have no further use.
ITEM_LISTS_DIR = "item_lists"
ITEM_LIST_KEEP_DAYS = 183
_ARCHIVED_ITEM_LIST = re.compile(r"^item_list_(\d{4}-\d{2}-\d{2})\.(?:xls|xlsx)$", re.IGNORECASE)


def archive_item_list(
    conn: sqlite3.Connection, folder: Path, src: Path, as_of: date, suffix: str
) -> str:
    """Keep a dated copy of an item list that just became the current one."""
    rel = f"{ITEM_LISTS_DIR}/{fy_folder(as_of)}/item_list_{as_of.isoformat()}{suffix.lower() or '.xlsx'}"
    dest = folder / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    st = dest.stat()
    db.upsert_watched_file(
        conn, rel, st.st_size, int(st.st_mtime), None, ITEM_LIST_STATUS,
        f"Item list dated {as_of.isoformat()}, kept for the record (the last 6 months are kept).",
    )  # fmt: skip
    return rel


def prune_item_list_archive(conn: sqlite3.Connection, folder: Path) -> list[str]:
    """Delete archived item lists more than ITEM_LIST_KEEP_DAYS older than
    the newest one. Measured from the newest list's date, not today, so a
    few months without uploads doesn't empty the archive. Only ever touches
    files this app wrote — item_lists/**/item_list_YYYY-MM-DD.xls(x) — never
    anything else someone put in uploads/. A file that can't be deleted
    (locked) is left for next time."""
    root = folder / ITEM_LISTS_DIR
    if not root.is_dir():
        return []
    dated = []
    for p in root.rglob("*"):
        m = _ARCHIVED_ITEM_LIST.match(p.name)
        if not (m and p.is_file()):
            continue
        try:
            dated.append((date.fromisoformat(m.group(1)), p))
        except ValueError:  # e.g. item_list_2026-13-01.xlsx — not one of ours
            pass
    if not dated:
        return []
    cutoff = max(d for d, _ in dated) - timedelta(days=ITEM_LIST_KEEP_DAYS)
    removed = []
    for d, p in sorted(dated):
        if d >= cutoff:
            continue
        try:
            p.unlink()
        except OSError:
            continue
        rel = p.relative_to(folder).as_posix()
        conn.execute("DELETE FROM watched_files WHERE filename = ?", (rel,))
        removed.append(rel)
    return removed


def _record_item_list(
    conn, result: SyncResult, folder: Path, path: Path, rel_name: str, filesize: int, mtime: int
) -> None:
    """An item list found in the folder: import it if it's newer than the
    current one (that's how one dropped straight into uploads/ gets used),
    otherwise just note it. Never replaces the current list with an older
    one — the dated copies under item_lists/ are older by definition."""
    result.item_lists.append(rel_name)
    try:
        df, meta = parse_item_list(str(path))
    except Exception as e:  # noqa: BLE001 — surfaced per-file, sync must not abort on one bad file
        result.errors.append((rel_name, str(e)))
        db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", str(e))
        return
    current = db.get_item_catalog_meta(conn)
    current_as_of = date.fromisoformat(current["as_of"]) if current else None
    if meta.as_of is None:
        detail = (
            "An item list with no date in its banner, so it can't be compared with the current one — "
            "upload it under Item code list on the Reports page instead."
        )
    elif df.empty:
        detail = "An item list with no items in it — not used."
    elif current_as_of is None or meta.as_of > current_as_of:
        # Commit what this scan has done so far first: if the item list
        # can't be imported (e.g. a code listed twice), rolling that back
        # must not also undo statements imported earlier in this scan.
        conn.commit()
        try:
            db.import_item_catalog(conn, df)
            db.set_item_catalog_as_of(conn, meta.as_of)
        except Exception as e:  # noqa: BLE001 — one bad file must not abort the rescan
            conn.rollback()
            msg = f"Couldn't import this item list ({e}). Check it, then upload it on the Reports page."
            result.errors.append((rel_name, msg))
            db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", msg)
            return
        result.item_lists_imported.append((rel_name, meta.as_of.isoformat()))
        detail = f"Imported as the current item list (dated {meta.as_of.isoformat()})."
        # The list is in; keeping copies of the file is best-effort — a
        # locked file (antivirus, Excel) just means no copy this time.
        try:
            if rel_name != ITEM_CATALOG_FILENAME:
                shutil.copyfile(path, folder / ITEM_CATALOG_FILENAME)
                st = (folder / ITEM_CATALOG_FILENAME).stat()
                db.upsert_watched_file(
                    conn, ITEM_CATALOG_FILENAME, st.st_size, int(st.st_mtime), None, ITEM_LIST_STATUS,
                    f"The current item list (dated {meta.as_of.isoformat()}).",
                )  # fmt: skip
            archive_item_list(conn, folder, path, meta.as_of, path.suffix)
            prune_item_list_archive(conn, folder)
        except OSError as e:
            detail += f" (Couldn't keep a copy of the file: {e}.)"
    elif meta.as_of == current_as_of:
        detail = f"The current item list (dated {meta.as_of.isoformat()})."
    else:
        detail = (
            f"An item list dated {meta.as_of.isoformat()} — older than the current one "
            f"({current_as_of.isoformat()}), so kept for the record but not used."
        )
    if (folder / rel_name).exists():  # the prune above may have just removed it
        db.upsert_watched_file(conn, rel_name, filesize, mtime, None, ITEM_LIST_STATUS, detail)


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

        try:
            stat = path.stat()
        except OSError as e:
            # A file that's locked (antivirus scanning it, open in Excel)
            # or vanished between the rglob() listing above and here (moved
            # or deleted mid-scan) must not abort the whole refresh — every
            # other file in the folder is still worth importing. Report just
            # this one and move on; it'll be picked up again next refresh
            # once it's no longer locked/missing.
            result.errors.append((rel_name, f"Could not read this file ({e})"))
            continue
        filesize, mtime = stat.st_size, int(stat.st_mtime)
        known = db.get_watched_file(conn, rel_name)
        if known is not None and known["filesize"] == filesize and known["mtime"] == mtime:
            # An unchanged file previously rejected as a broken stock
            # statement may really be the item list (uploads/item_catalog.xlsx
            # was, on every rescan) — re-check just those, so the false
            # "rejected file" clears itself instead of lingering forever.
            if known["status"] == "error" and is_item_list(str(path)):
                _record_item_list(conn, result, folder_path, path, rel_name, filesize, mtime)
            else:
                result.unchanged.append(rel_name)
            continue

        # The POS item list lives in uploads/ too (the Reports page saves it
        # there). It's not a stock statement, so don't report it as a broken
        # one.
        if is_item_list(str(path)):
            _record_item_list(conn, result, folder_path, path, rel_name, filesize, mtime)
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
            msg = (
                "No SKU rows could be read from this file — it may not be a stock statement export"
            )
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
        if existing_report_id is not None and (
            known is None or known["report_id"] != existing_report_id
        ):
            # This period is already in the database from a different file.
            result.duplicate_period.append(
                (rel_name, meta.period_start.isoformat(), meta.period_end.isoformat())
            )
            db.upsert_watched_file(
                conn, rel_name, filesize, mtime, existing_report_id, "duplicate_period"
            )
            continue

        # Overlaps a report imported from a more recently saved file — that
        # newer file wins, so this one is skipped rather than deleting it
        # (see db.find_newer_overlapping_source). Fingerprinted with the
        # winning report's id, same convention as duplicate_period above:
        # an unchanged file is then skipped on every later scan, and if that
        # winning report is ever removed or replaced, delete_report clears
        # this fingerprint too, so the file gets reconsidered.
        newer = db.find_newer_overlapping_source(conn, meta, mtime)
        if newer is not None:
            period = f"{meta.period_start.isoformat()} to {meta.period_end.isoformat()}"
            result.superseded.append((rel_name, period, newer["filename"]))
            db.upsert_watched_file(
                conn,
                rel_name,
                filesize,
                mtime,
                newer["report_id"],
                "superseded",
                f"Covers {period}, which overlaps {newer['period_start']} to "
                f"{newer['period_end']} from {newer['filename']} — a more recently saved file, "
                "so that one is used instead. Delete this file if it's no longer needed.",
            )
            continue

        try:
            import_result = db.import_report(
                conn, df, meta, rel_name, replace=(existing_report_id is not None)
            )
        except ValueError as e:  # e.g. an unparseable file slipping past the checks above
            result.errors.append((rel_name, str(e)))
            db.upsert_watched_file(conn, rel_name, filesize, mtime, None, "error", str(e))
            continue
        db.upsert_watched_file(conn, rel_name, filesize, mtime, import_result.report_id, "imported")
        result.imported.append(
            (rel_name, meta.period_start.isoformat(), meta.period_end.isoformat(), len(df))
        )

    return result
