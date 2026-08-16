# Agent 7 — Dead Code Removal — Summary

## Method
Read `.cleanup/00-baseline.md` and `agent-1-summary.md` through `agent-6-summary.md` first.
Agent 6's summary flagged the new TypedDicts (`ThresholdValues`, `Settings`, `CoverageGap`,
`ImportGaps`, `DeadStockAgingBucket`, `ValueSegmentSummary`, `CatalogMeta`) added to
`analysis.py`/`db.py` — kept these in mind as likely false-positive "unused" targets for
static tools, since `TypedDict` fields are structural (matched by key name in dict literals,
never referenced as Python attributes) and vulture/ruff can't see that usage.

Ran two tools for candidates:
- `uv run --with vulture vulture src app.py main.py --min-confidence 60`
- `ruff check --select F401,F841 .` (unused imports/unused local variables)

Then manually cross-referenced **every single finding** against the dynamic-runtime boundary
checklist and real usage before considering any deletion, plus did an independent manual sweep
(grep every `def`/`class` name across the whole repo — `.py` and `.html` — counting references)
to catch anything the tools might have missed.

## Findings

### `ruff check --select F401,F841 .`
**Zero findings.** No unused imports, no unused local variables anywhere in the tracked source.

### `vulture` (24 findings, all investigated, all confirmed false positives)

1. **`analysis.py:304` `_status` — flagged "unused function".**
   Checked: not called from any production code path (confirmed by grep — only referenced in
   comments within `analysis.py` itself). **But** it is directly imported and called by
   `tests/test_status.py` (11 call sites) and `tests/test_status_vectorization_consistency.py`
   (1 call site), whose own docstring explains exactly why: `_status()` was the original
   if/elif implementation, later replaced in the production path by a vectorized `np.select`
   for performance, but **deliberately kept as a small readable reference oracle** — the
   vectorization-consistency test runs real data through both implementations and asserts they
   agree, which is the actual enforcement that the fast path and the reference path never
   silently drift apart. This is a real, load-bearing test fixture, not dead code. **Not
   removed.**

2. **`analysis.py:74-75` `version`/`updated_at` fields on the `Settings` TypedDict — flagged
   "unused variable".** Checked: `TypedDict` field declarations are structural type
   annotations, not runtime variables; vulture doesn't understand this and treats them like
   local assignments. Verified both keys are actively read/written as dict keys in `db.py`
   (`db.py:492` `{**DEFAULT_SETTINGS, "version": 0, "updated_at": None}`, `db.py:500-501` in
   the settings-row column list). **Not removed** — a TypedDict field is documentation/type
   contract, and it's demonstrably in active use.

3. **`db.py:643` `imported_at` field on `CatalogMeta` TypedDict — same false-positive pattern
   as #2.** Verified: `db.py:654` `return {"imported_at": row[0], ...}` and
   `webapp.py:124` `"imported_at": str(r["imported_at"])` both actively use the key. **Not
   removed.**

4. **`db.py:231/238/435/437` `row_factory` — flagged "unused attribute" (×4, two call
   sites).** Checked: `conn.row_factory = sqlite3.Row` / `conn.row_factory = None` in
   `get_report`/`get_watched_file` is a real, functional toggle of sqlite3's cursor-row
   behavior (changes whether `.fetchone()` returns a plain tuple or a `sqlite3.Row` supporting
   name-based column access) — vulture can't see that assigning to this attribute has a
   runtime effect on subsequent calls in the same function. **Not removed** — Agent 2 already
   looked at this exact pattern (in the dedup pass) and correctly declined to touch it for a
   different reason (over-abstraction risk); reconfirmed here it's also not dead.

5. **`excel_export.py:100` `width` — flagged "unused attribute".**
   `ws.column_dimensions[...].width = min(length + 2, 40)` — sets openpyxl's column-width
   property for its side effect on the written `.xlsx` file (auto-fit columns in the exported
   spreadsheet). Assignment-only attributes on a third-party object are invisible to vulture's
   usage tracking but are exactly how openpyxl's API works. **Not removed.**

6. **`webapp.py` — 15 functions flagged "unused"**: `inject_globals`, `check_csrf`,
   `handle_403`, `handle_too_large`, `dashboard`, `trends`, `api_search`, `api_sku_detail`,
   `api_brand_detail`, `api_reports`, `api_import_health`, `api_save_settings`,
   `api_remove_report`, `api_upload`, `api_upload_item_list`, `api_refresh`.
   **Dynamic-boundary check performed on every one**: all 15 are registered via Flask
   decorators (`@app.context_processor`, `@app.before_request`, `@app.errorhandler(403)`,
   `@app.errorhandler(413)`, `@app.get(...)` ×9, `@app.post(...)` ×4 — confirmed by re-reading
   every decorator line in `webapp.py`). These are dispatched by Flask's URL router / hook
   system at request time, never via a static Python call vulture can trace — this is exactly
   the false-positive pattern called out in the task brief's checklist. **Not removed.**

### Manual sweep beyond the tools
- Grepped every `def`/`class` declaration in all 7 `src/gowri_proj/*.py` modules and counted
  references (by exact name) across every `.py` and `.html` file in the repo (excluding
  `.venv`). **Zero symbols came back with fewer than 2 total references** (i.e., every
  declared function/class is used somewhere beyond its own definition line) — no additional
  candidates surfaced.
- Checked every module (`analysis`, `dashboard`, `db`, `excel_export`, `parser`, `sync`,
  `webapp`) is imported from at least one other file — all 7 are (matches Agent 5's confirmed
  import graph; no orphaned modules).
- Checked all 5 templates under `src/gowri_proj/templates/` (`base.html`, `dashboard.html`,
  `trends.html`, `settings.html`, `reports.html`) are each reachable via a `render_template(...)`
  call in `webapp.py` — confirmed for `dashboard.html`, `trends.html`, `settings.html`,
  `reports.html` directly; `base.html` is Jinja's `{% extends %}` base layout, used implicitly
  by the other four (grep-confirmed `{% extends "base.html" %}` appears in each).
- Checked `src/gowri_proj/dashboard_template.html` (a second, separate HTML template outside
  `templates/`) — not orphaned: it's read directly via `Path(__file__).with_name(...)` in
  `dashboard.py`'s standalone CLI-export path (`write_dashboard`), a different rendering
  mechanism from the Flask app's Jinja templates (confirmed by reading `dashboard.py`'s own
  comment explaining the split: "the standalone CLI export ... needs both").
- Checked `output/inventory_dashboard.html` — gitignored generated artifact (`.gitignore:14`
  covers `output/`), not a tracked source file; no action relevant to this pass.
- Checked install/run/update `.command`/`.bat` scripts at repo root — none reference any
  Python symbol this pass considered removing (they invoke `main.py`/the venv, nothing more
  granular).
- Searched for commented-out code (`grep` for `#` lines containing `def `/`class `/`import `)
  — found only prose comments referencing function names in explanatory text (e.g. "see
  `_status()`'s docstring"), no disabled/commented-out code blocks anywhere.

## Changes implemented
**None.** Every candidate surfaced by `vulture` and `ruff` was investigated and is either a
genuine false positive (Flask dynamic dispatch, TypedDict structural fields, side-effecting
attribute assignment) or deliberately-kept test infrastructure (`_status` as a reference
oracle). No unreachable code, unused imports, unreferenced files, or dead branches were found
anywhere in the tracked source. The codebase is already dead-code-free at this point in the
pipeline — consistent with Agent 2's earlier finding that the codebase was "already tightly
written" and Agent 4's finding that the test suite has no dead-code tests.

## Files touched
None (source unchanged). Only this handoff file is new.

## Validation (FULL tier, as assigned)
Re-ran to reconfirm baseline health since no code changed:
- `uv sync`: resolves cleanly, 24 packages (unchanged from Agent 6's handoff).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (unchanged).
- `ruff check .`: All checks passed.
- `ruff format --check .`: 35 files already formatted.

## Recommendations NOT implemented
None — there was nothing actionable to defer. If a human wants stronger dead-code guarantees
going forward, `vulture` could be adopted as a permanent CI check with a curated
`# noqa`-equivalent allowlist (vulture supports a whitelist file) for the known false-positive
categories above (Flask routes/hooks, TypedDict fields, `row_factory`/openpyxl attribute
assignments) — not implemented here since the task brief said not to add it as a permanent
dependency, and a bare `vulture` gate without that allowlist would create constant noise.

## Notes for Agent 8 (Error Handling Audit)
- `webapp.py`'s 3 `# noqa: BLE001` suppressions on broad `except Exception as e:` blocks
  (1 in `sync.py`, 2 in `webapp.py`, per Agent 1's summary) are still present and were
  re-confirmed still legitimate as of this pass (unchanged, not re-litigated here — Agent 1
  already verified they're needed against ruff's active rule set). Worth Agent 8 taking a
  fresh look at *why* each blind-except exists (error message content, whether the exception
  is re-raised/logged/swallowed) as part of the error-handling audit proper, since this pass
  didn't evaluate their handling quality, only confirmed they're not unreachable dead code.
- `tests/test_webapp_catalog_upload.py` mocks `os.replace` to simulate a disk-full/permissions
  failure on the upload rollback path (per Agent 4's summary) — that's the one concrete
  existing test of an error path in `webapp.py`; Agent 8 may find it useful as a reference for
  what's already covered vs. what other error paths (e.g. the 403/413 handlers, or the other
  broad-except blocks) still lack direct tests.
- No functions, files, or imports were removed in this pass, so Agent 8 doesn't need to
  reconcile any newly-missing symbols against error-handling code that referenced them.
