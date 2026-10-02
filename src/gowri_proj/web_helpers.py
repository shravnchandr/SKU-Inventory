"""Request-handling helpers shared by the web routes: upload staging and
Windows file-lock retries, CSV responses, report serialisation, and the
small pure helpers behind a few API fields (item-list reminder, data-quality
fingerprint, a SKU's value segment)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pandas as pd
from flask import Response, jsonify
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from .analysis import (
    InventorySummary,
)
from .dashboard import (
    SEGMENT_MOVEMENT_LABELS,
    SEGMENT_TIER_LABELS,
    _segment_policy,
)


def _short_date(d: date, other: date) -> str:
    """'9 Aug', with the year only when the two dates being compared differ in it."""
    s = f"{d.day} {d.strftime('%b')}"
    return s if d.year == other.year else f"{s} {d.year}"


def item_list_reminder(catalog_meta: dict | None, latest_report_end: date | None) -> dict | None:
    """A nudge to upload a fresh item list, or None if it's up to date.

    Rename detection leans on the item list's codes, and it only sees a
    rename once an item list from *after* that rename has been uploaded —
    so the list is meant to be uploaded every month, alongside the stock
    statement. "Older than the latest report" is the signal it's been missed.
    """
    if latest_report_end is None:
        return None
    if catalog_meta is None:
        return {
            "kind": "missing",
            "message": "No item list uploaded yet. Upload one so renamed items are recognised automatically.",
        }
    as_of = date.fromisoformat(catalog_meta["as_of"])
    if as_of >= latest_report_end:
        return None
    return {
        "kind": "stale",
        "item_list_as_of": as_of.isoformat(),
        "latest_report_end": latest_report_end.isoformat(),
        "message": (
            f"Your item list is from {_short_date(as_of, latest_report_end)} and the latest report is "
            f"{_short_date(latest_report_end, as_of)}. Upload a fresh one so renames are recognised automatically."
        ),
    }


def _value_segment_for(summary: InventorySummary, sku: str) -> dict | None:
    """The ABC-tier x movement segment for one SKU, or None if it isn't
    segmented (out_of_stock/returned, or not currently stocked at all —
    see analysis._compute_value_segments)."""
    rows = summary.value_segment_skus[summary.value_segment_skus["sku"] == sku]
    if rows.empty:
        return None
    row = rows.iloc[0]
    return {
        "tier": row["tier"],
        "movement": row["movement"],
        "tier_label": SEGMENT_TIER_LABELS[row["tier"]],
        "movement_label": SEGMENT_MOVEMENT_LABELS[row["movement"]],
        "policy": _segment_policy(row["tier"], row["movement"]),
    }


def _validate_upload(file: FileStorage | None) -> tuple[str | None, tuple | None]:
    """(filename, None) for a present, non-empty .xls/.xlsx upload, or
    (None, <error response tuple>) to return straight from the route.
    Shared by /api/upload and /api/upload-item-list — both accept the same
    file shape, they just do different things with it afterward.
    """
    if not file or not file.filename:
        return None, (jsonify(error="No file received."), 400)
    filename = secure_filename(file.filename)
    if not filename.lower().endswith((".xls", ".xlsx")):
        return None, (jsonify(error="Only .xls or .xlsx files are supported."), 400)
    return filename, None


@contextmanager
def _staged_upload(file: FileStorage, filename: str, uploads_dir: Path):
    """Save `file` to a dotfile-named temp path inside uploads_dir (same
    filesystem as the eventual destination, so the final move can be an
    atomic os.replace) and yield that path — validate the saved file
    *before* it ever becomes the real, visibly-named file, so a rejected
    upload can't overwrite an existing report's/catalog's provenance.
    Always removes the temp file on the way out; a no-op if the caller
    already os.replace'd it into its final destination.
    """
    uploads_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".upload-", suffix=Path(filename).suffix, dir=uploads_dir
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            file.save(f)
        yield tmp_path
    finally:
        try:
            tmp_path.unlink(missing_ok=True)  # no-op if os.replace already moved it into place
        except OSError:
            # If something still has this temp file locked — the same
            # reason a caller's _replace_with_retry may have just given up
            # and returned its clear, specific error — don't let a failed
            # *cleanup* raise here and clobber that response in flight with
            # a bare, generic one instead (confirmed: an exception raised
            # from a @contextmanager's finally during __exit__ replaces a
            # return already pending inside the `with` block). It's a
            # dotfile (sync.py's _is_candidate skips anything starting with
            # "." or "~$"), so leaving it behind doesn't get picked up as a
            # report — just cleaned up by hand, or the next time this path
            # isn't locked.
            pass


def _retry_transient_lock(fn, *, attempts: int = 10, delay: float = 0.75):
    """Call fn(), retrying briefly on a transient PermissionError before
    giving up and letting it raise.

    Windows (unlike POSIX, which doesn't care who else has a file open)
    refuses some filesystem operations — replacing a file, occasionally even
    just stat'ing one — while another process momentarily has it locked.
    Most commonly that's antivirus real-time scanning grabbing a
    just-written file for a moment, usually over in well under a second —
    but reproduced live with the original ~2s retry budget (5 attempts,
    0.4s apart) still not being enough on a real machine, so this is
    ~6.75s now (10 attempts, 0.75s apart): still bounded — a genuinely
    stuck lock (the file actually open in Excel, or in an Explorer preview
    pane, which do NOT clear on their own) fails in a few seconds rather
    than hanging the request, but with real headroom for a slower scan
    rather than assuming under a second is always enough. Used for the
    filesystem calls in the upload path that have actually hit this live
    (os.replace) or sit right next to it (stat'ing the same file
    immediately after) — not for calls that aren't meaningfully exposed to
    it, like writing a brand-new uniquely-named temp file (nothing else has
    ever had a handle on it) or creating a directory (not the kind of thing
    a scanner holds a lock on).
    """
    if attempts < 1:
        # attempts=0 would skip the loop entirely, leaving last_error at
        # None below and turning `raise last_error` into `raise None` — a
        # bare TypeError that hides what actually went wrong. Every call
        # site today uses the default of 5, so this can't fire in practice;
        # it's here so a future caller passing an accidental 0 fails
        # immediately and legibly instead.
        raise ValueError(f"attempts must be at least 1, got {attempts!r}")
    last_error: PermissionError | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except PermissionError as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(delay)
    raise last_error


def _replace_with_retry(src: Path, dest: Path, attempts: int = 10, delay: float = 0.75) -> None:
    """os.replace, retrying briefly on a transient PermissionError (see
    _retry_transient_lock) before raising a plain message instead of a bare
    WinError.

    Reproduced live twice now: a real upload 500'd with `PermissionError:
    [WinError 5] Access is denied` on this exact os.replace, saving a
    freshly-written temp file over an existing report file — the second
    time against the same destination file as the first, and outlasting the
    original ~2s retry budget, which is what motivated widening it (see
    _retry_transient_lock). If it's still locked after retrying, something
    more persistent has it open — most plausibly, given a transient AV scan
    should have cleared well inside this window, the destination file
    itself sitting open in Excel or an Explorer preview pane — and this
    raises so the caller can report that plainly instead of the raw error
    reaching the user as an unqualified 500.
    """
    try:
        _retry_transient_lock(lambda: os.replace(src, dest), attempts=attempts, delay=delay)
    except PermissionError as e:
        raise OSError(
            f"Windows wouldn't let this file be saved ({e}). This usually means another "
            "program has it locked — antivirus scanning it, the file open in Excel, or even just "
            "showing in an Explorer preview pane. Close anything that might have it open "
            "(including any Explorer window previewing it) and try uploading again."
        ) from e


def _serialize_reports(reports_df: pd.DataFrame) -> list[dict]:
    if reports_df.empty:
        return []
    out = []
    for _, r in reports_df.iterrows():
        out.append(
            {
                "id": int(r["id"]),
                "company": r["company"],
                "location": r["location"],
                "period_start": r["period_start"].date().isoformat(),
                "period_end": r["period_end"].date().isoformat(),
                "period_days": int(r["period_days"]),
                "sku_count": int(r["sku_count"]),
                "source_filename": r["source_filename"],
                "imported_at": str(r["imported_at"]),
            }
        )
    return out


def _csv_response(rows: list[dict], columns: list[tuple[str, str]], filename: str) -> Response:
    """A downloadable CSV attachment from a list of dicts. Used for the
    "download the full list" links on Reports — those need every row, not
    just the display-capped subset /api/import-health returns to keep the
    page itself light, so they're built here from an uncapped query rather
    than by exporting whatever the page happens to have already fetched
    (the client-side exportCsv in dashboard.html, which works from a page
    that already loaded everything up front).

    The BOM prefix matches dashboard.html's own exportCsv — without it,
    Excel guesses the wrong encoding for anything outside plain ASCII.
    """
    lines = [",".join(csv_cell(label) for _, label in columns)]
    for row in rows:
        lines.append(",".join(csv_cell(row.get(key, "")) for key, _ in columns))
    csv_text = "﻿" + "\r\n".join(lines)
    return Response(
        csv_text,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def quality_issues_fingerprint(issues: list[dict]) -> str | None:
    """A short, stable ID for one exact set of data-quality issues — what the
    Reports page's "Dismiss" remembers. Keyed on the *set* (sorted, so the
    order issues happen to be listed in can't change it), not a count: a
    later import that fixes one issue and introduces a different one has
    the same count but must still bring the warning back.
    """
    if not issues:
        return None
    keys = sorted((str(i["brand"]), str(i["sku"]), i["period_end"], i["issue"]) for i in issues)
    return hashlib.sha256(json.dumps(keys).encode()).hexdigest()[:16]


def csv_cell(value: object) -> str:
    s = "" if value is None else str(value)
    if any(c in s for c in ('"', ",", "\n")):
        s = '"' + s.replace('"', '""') + '"'
    return s
