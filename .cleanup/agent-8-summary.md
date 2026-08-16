# Agent 8 — Error Handling Audit — Summary

## Method
Read `.cleanup/00-baseline.md` and `agent-1-summary.md` through `agent-7-summary.md` first.
Agent 1's summary flagged 3 pre-existing `# noqa: BLE001` suppressions on `except Exception`
blocks (1 in `sync.py`, 2 in `webapp.py`) as verified-legitimate against ruff's rule set; Agent 7
re-confirmed they're reachable (not dead code) but explicitly deferred judging their *handling
quality* to this pass. Re-verified all three independently below, plus every other defensive
construct in the codebase.

Searched `app.py`, `main.py`, `src/gowri_proj/*.py` for `try:`/`except`/bare-except/
`contextlib.suppress`/trailing `pass` via grep. Found and individually evaluated every
try/except construct in the tracked source (10 total — no bare `except:`, no
`contextlib.suppress`, no silent `pass`-only handlers anywhere).

## Every try/except found, with judgment

1. **`main.py:48-54`** (`cmd_import`) — catches `ValueError` from `db.import_report`, prints a
   user-facing "Not imported: ..." message, returns cleanly. **Legitimate**: CLI entry point
   converting a documented business-rule rejection (e.g. partial-overlap conflict) into a clean
   exit instead of a raw traceback. Added a one-line comment explaining this (none existed).
2. **`main.py:162-165`** (`_bounded_days`) — catches `ValueError` from `int(value)` on a raw CLI
   string, re-raises as `argparse.ArgumentTypeError`. **Legitimate**: handling truly unknown
   external input (a user-typed CLI flag), converting to argparse's expected error protocol.
   Already has a full docstring explaining purpose. No change.
3. **`sync.py:85-90`** (`sync_folder`) — catches `Exception` around `parse_stock_statement` for
   one file in a folder scan, records the error in `result.errors` and persists an "error" status
   in the DB, then `continue`s to the next file. **Legitimate, not swallowed**: the exception is
   recorded (not discarded) and batch processing must not abort on one malformed file among many
   — this is exactly the "documented partial-failure-must-not-crash-everything" case. Existing
   `noqa: BLE001` comment already explains why. No change.
4. **`sync.py:143-150`** — catches `ValueError` from `db.import_report` per-file during sync,
   same record-and-continue pattern as #3. **Legitimate**, already commented. No change.
5. **`db.py:205-209`** (`connect` context manager) — `try: yield conn; conn.commit() finally:
   conn.close()`. **Legitimate**: standard resource-cleanup idiom, no except clause — exceptions
   from the caller's `with` block propagate untouched; this only guarantees the connection is
   always closed. No change needed.
6. **`webapp.py:101-106`** (`_staged_upload` context manager) — `try: ... yield tmp_path finally:
   tmp_path.unlink(...)`. **Legitimate**: same resource-cleanup idiom as #5, guarantees the temp
   upload file is removed on every exit path. Docstring above already explains the pattern. No
   change.
7. **`webapp.py:418-421`** (`api_save_settings`) — catches `(TypeError, ValueError)` from
   `int(raw)` on a JSON request-body field, returns 400 with a field-specific message.
   **Legitimate**: validating untrusted HTTP request input at a Flask API boundary. No change.
8. **`webapp.py:457-460`** (`api_upload`) — catches `Exception` around `parse_stock_statement` on
   an uploaded file, returns 422 with the error text. **Legitimate**: uploaded files are
   arbitrary external input (could be a corrupt/wrong-format .xlsx); a parse failure must surface
   to the user as a rejected upload, not crash the request. Already has `noqa: BLE001` explaining
   this. No change.
9. **`webapp.py:548-551`** (`api_upload_item_list`) — same pattern as #8 for `parse_item_list`.
   **Legitimate**, already commented. No change.
10. **`webapp.py:567-582`** (`api_upload_item_list`, `os.replace` step) — catches `OSError`
    specifically (not broad `Exception`), and on failure explicitly rolls back the DB import that
    already committed (`db.rollback_item_catalog`) so DB and disk stay consistent, then returns
    500 with a clear message. **Legitimate**: real filesystem I/O can fail (disk full,
    permissions) after the DB write already succeeded; this is precisely the
    documented-partial-failure-must-not-corrupt-state case, and it's the one error path with a
    dedicated test (`tests/test_webapp_catalog_upload.py`, mocks `os.replace` per Agent 7's note).
    Already thoroughly commented. No change.

## Removals

**None.** Every try/except in the codebase handles truly unknown external input (uploaded files,
CLI arguments, HTTP request bodies), is a standard resource-cleanup try/finally with no except
clause, or is a documented business-rule rejection that is recorded/surfaced rather than
discarded. No bare `except:`, no `contextlib.suppress`, no logging-and-continuing-with-no-recovery
patterns, and no try/except wrapping code that provably cannot throw were found. The 3
`noqa: BLE001` suppressions flagged by Agent 1 were independently re-verified here on handling
quality (not just reachability): all three record the exception into a result/response rather
than discarding it, none hide a programmer bug, and all sit at genuine external-input boundaries
(per-file batch sync, uploaded-file parsing). Confirmed legitimate a second time, by a different
agent, on different grounds (Agent 1 checked ruff-rule necessity; this pass checked
swallow-vs-surface behavior).

## Documented-only for human review

None — nothing rose to even a borderline case; every construct had a clear, individually
verifiable justification (either genuinely-unknown external input, mandatory Flask error-boundary
registration, or resource cleanup with no except clause at all).

## Changes implemented
- `main.py`: added one explanatory comment to the `except ValueError` in `cmd_import` (no comment
  existed there previously; every other kept except-block already had one). Pure comment
  addition, zero behavior change.

## Files touched
- `main.py`: +3/-0 lines (comment only)
- `.cleanup/agent-8-summary.md`: new file (this handoff)

No files in `src/gowri_proj/` were modified — every construct there already carried adequate
justification in-place.

## Validation (FULL tier, as assigned)
- `uv sync`: resolves cleanly, 24 packages (unchanged).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (unchanged from Agent 7's handoff — comment-only
  change, no behavior touched).
- `ruff check .`: All checks passed.
- `ruff format --check .`: 36 files already formatted.

## Notes for Agent 9 (Legacy & Fallback Code Removal)
- No fallback-on-error patterns (return-None-instead-of-raising, catch-and-default) exist in the
  codebase — every except block in the audit above surfaces or records the error rather than
  silently substituting a fallback value. Agent 9 should not expect to find "the error handling
  audit already cleaned up my targets" gaps to fill — this pass found nothing to remove.
- The `_staged_upload`/`connect` try/finally resource-cleanup patterns (webapp.py:101, db.py:205)
  are not fallback code — don't mistake them for legacy patterns; they're the only correct way to
  guarantee cleanup on early-exit/exception paths in this codebase's structure.
- `os.replace` + rollback pattern in `webapp.py:567-582` is the one place state can partially
  diverge (DB committed, file write failed) and it already has full remediation — not a
  legacy/fallback smell, it's the safety net for a real dual-write hazard.
