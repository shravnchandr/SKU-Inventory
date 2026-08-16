# Agent 2 — Deduplication & DRY — Summary

## Scan scope
Read every module in `src/gowri_proj/` (`analysis.py`, `db.py`, `dashboard.py`,
`webapp.py`, `parser.py`, `sync.py`, `excel_export.py`), plus `app.py` and
`main.py`, and the `tests/` directory listing. `analysis.py` is the largest
module (1046 lines) but is already tightly written with no duplicate logic
blocks — its per-status/per-segment helpers each do genuinely distinct work.

## Findings

### Implemented (high-confidence, logically identical)

1. **`src/gowri_proj/parser.py` — `parse_meta` vs `parse_item_list_meta`**
   (formerly lines 79-102 and 139-159). Both looped over `raw.head(10)`,
   pulling the first banner text as `company`, the next non-matching banner
   text as `location`, and testing every banner line against a regex —
   identical control flow, differing only in which regex/date-format/output
   fields were used. Extracted the shared scan into `_scan_banner(raw,
   pattern) -> (company, location, match)`; both callers now just interpret
   the returned `match` differently (2 groups -> period_start/period_end,
   1 group -> as_of). Not reached via any dynamic dispatch — both are plain
   function calls from `parse_stock_statement`/`parse_item_list`.

2. **`src/gowri_proj/webapp.py` — `/api/upload` vs `/api/upload-item-list`**
   (formerly lines 407-431 and 512-535 for the shared prefix, plus each
   route's own `finally: tmp_path.unlink(...)` at the end). Both routes had
   byte-for-byte identical file-validation logic (empty-file check,
   `secure_filename`, `.xls`/`.xlsx` extension check, identical error
   messages) and identical stage-then-atomic-replace temp-file handling
   (`tempfile.mkstemp` under a dotfile prefix in `uploads_dir`, write via
   `os.fdopen`, `finally: tmp_path.unlink(missing_ok=True)`). Extracted two
   helpers:
   - `_validate_upload(file) -> (filename, None) | (None, error_tuple)`
   - `_staged_upload(file, filename, uploads_dir)` — a `@contextmanager`
     wrapping the mkstemp/save/yield/finally-unlink sequence (verified the
     context-manager form preserves the original try/finally exception
     propagation exactly).
   Both route handlers are registered via Flask decorators (`@app.post(...)`)
   — confirmed the *registration* itself is untouched; only the shared body
   logic inside each handler was factored out, so no dynamic-dispatch
   boundary is affected. Response shapes, status codes, and error messages
   are unchanged (verified by diff and by the full test suite, which
   includes `tests/test_webapp_catalog_upload.py`).

### Recommendations NOT implemented (documented, not high-confidence enough or too small to be worth the risk)

- **`src/gowri_proj/db.py`** — `get_report` (line ~227) and `get_watched_file`
  (line ~431) both do `conn.row_factory = sqlite3.Row` / fetch / `conn.row_factory
  = None`. Only 2 occurrences, each wrapping a different single-row query;
  extracting a helper would trade ~4 lines of boilerplate per site for an
  extra layer of indirection around a stdlib idiom — judged over-abstraction
  for this little duplication, left as-is.
- **`src/gowri_proj/parser.py`** — the `not started` header-validation blocks
  in `parse_stock_statement` and `parse_item_list` have a similar shape (look
  for a header marker, then cross-check a couple more fixed columns before
  trusting the layout, else raise a descriptive `ValueError`) but differ in
  which columns/labels they check and the exact wording of each error
  message. Not logically identical (different arity of columns checked,
  different messages) — left alone rather than force a shared abstraction
  that would need parameterizing 4+ things for 2 call sites.
- **`src/gowri_proj/analysis.py`** — `_row_map`/`_rows` closures inside
  `find_sku_churn`, and the `subset()` closure inside `summarize_history`,
  are local single-use helpers, not duplicated elsewhere; left as-is (no
  duplication to fix).
- Did not touch `tests/` — no `conftest.py` exists and the 16 test files
  looked independent by naming/content; a closer per-test dedup pass was out
  of scope for the highest-impact, lowest-risk bar this pass set, and
  touching test fixtures carries more behavior-change risk for less payoff
  than the two changes above.

## Files touched
- `src/gowri_proj/parser.py` (+21/-24 net, small: one new shared helper,
  two callers simplified)
- `src/gowri_proj/webapp.py` (+... net roughly neutral: two new helpers
  added near the top, ~90 lines of duplicated route-handler boilerplate
  removed across the two upload routes)
- `git diff --stat`: 2 files changed, 70 insertions(+), 65 deletions(-)

## Validation (FULL tier, as assigned)
- `uv sync`: resolves cleanly, 23 packages, no changes to dependencies.
- `.venv/bin/pytest -q`: **93 passed**, 0 failed — same count as Agent 1's
  baseline. Runtime ~1.2s.
- `ruff check .`: All checks passed.
- `ruff format --check .`: all files formatted (ran `ruff format .` once
  after the parser.py edit to pick up one line-wrap; verified idempotent
  afterward).

## Notes for Agent 3 (Dependency Audit)
- No dependency changes made in this pass (`uv.lock` untouched).
- The two new helpers in `webapp.py` (`_validate_upload`, `_staged_upload`)
  are module-level closures over `Flask`/`Path`/`tempfile`/`os` — no new
  imports beyond `contextlib.contextmanager` (stdlib, already implicitly
  available, now explicitly imported).
- No behavioral changes were made; every consolidation was verified against
  concrete evidence of logical identity (verbatim-matching code blocks), not
  style similarity — per the HIGH-CONFIDENCE BAR constraint. If you spot
  further duplication candidates, the "recommendations not implemented"
  section above is a reasonable starting list of what was considered and
  passed over, with reasoning.
