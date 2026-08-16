# Agent 13 — Database & Persistence Audit — Summary

## Overall result
**No structural defects found.** Schema (`db.py:SCHEMA`) agrees with every read/write site across
`sync.py`, `webapp.py`, `main.py`, `analysis.py`, `dashboard.py`, `parser.py` — traced every
column/table reference back to its `CREATE TABLE`/`ALTER TABLE` origin, and every function's type
hint (Agent 6's work) against the SQL it actually runs. No purely-structural fix qualified (no
docstring/type-hint drift found to correct), so **no code changes were made** — same outcome shape
as Agent 12's report-only pass, for the same reason: nothing to fix without touching schema/data
behavior. This pass exists to document findings for human review, per the task's "when in doubt,
document, don't implement" instruction.

## Method
Read `.cleanup/00-baseline.md` through `agent-12-summary.md` first. Agent 12 flagged two specific
pointers for this pass (both followed up on below): the f-string SQL in `get_item_catalog_df`, and
a request to give `_migrate()`/`_dedupe_stock_entries()` (`db.py:138-194`) a full read.

Then, systematically:
1. Read all of `db.py` end to end (666 lines) — schema, migration logic, every query function.
2. Grepped every table/column name (`reports`, `stock_entries`, `watched_files`, `settings`,
   `item_catalog`, `item_name_changes`, and each of their columns) across `sync.py`, `webapp.py`,
   `main.py`, `analysis.py`, `dashboard.py`, `parser.py` to confirm every read/write site agrees
   with what `SCHEMA` actually creates.
3. Cross-checked every `db.py` function's return type hint (added by Agent 6) against the real
   shape of the SQL/DataFrame it returns.
4. Checked `tests/` for DB fixtures that hard-code a schema, to see if any had drifted from the
   real one.
5. Checked whether this is genuinely a no-external-consumer app (confirmed, see below) before
   judging whether "unused by app code" columns matter.

## Schema/model consistency — clean
No drift found anywhere. Specifically verified:
- `reports` columns (`company`, `location`, `period_start`, `period_end`, `period_days`,
  `source_filename`, `imported_at`) — all read and written consistently across `db.py`,
  `webapp.py`, `analysis.py`, `dashboard.py`, and the templates (`location` in particular is used
  in `dashboard.html`/`trends.html`'s meta line — confirmed not dead despite being an easy column
  to suspect of disuse).
- `stock_entries`'s 9 numeric columns match `_STOCK_ENTRY_NUMERIC_COLS` (used by
  `_dedupe_stock_entries`) and the `cols` list in `import_report` exactly — no silent
  reordering/omission.
- `item_catalog`'s 9 data columns match `ITEM_CATALOG_COLUMNS` (db.py) and `parser.py`'s
  `IL_COL_*`/output dict keys exactly — traced `parse_item_list` field-by-field.
- `watched_files` columns (`filesize`, `mtime`, `report_id`, `status`, `detail`, `checked_at`) —
  all round-tripped correctly through `upsert_watched_file`/`get_watched_file`/
  `list_watched_files_problems`, and surfaced in `reports.html`'s "files rejected on last scan"
  table.
- `settings` — `get_settings`'s `SELECT` column list, `upsert_settings`'s `INSERT`/`ON CONFLICT`
  column list, and the `Settings`/`ThresholdValues` TypedDicts (Agent 6) all agree on the same six
  threshold fields plus `version`/`updated_at`.
- `item_name_changes` — append-only rename log, columns match between `INSERT` (in
  `import_item_catalog`) and `SELECT` (in `get_name_change_map`).
- Every `db.py` function's return type hint matches its actual SQL/DataFrame shape (checked all
  25 top-level functions) — no hint drift for this agent to correct.

## Migration/schema-evolution logic assessment (`db.py:138-194`, per Agent 12's request)
`_migrate()` is sound, in the correct order:
1. `PRAGMA table_info(settings)` column-existence check before each `ALTER TABLE ADD COLUMN`
   (`value_tier_a_pct`, `value_tier_b_pct`) — correctly idempotent, won't error on a DB that
   already has these columns (added after `settings` first shipped).
2. `_dedupe_stock_entries()` runs **before** the `CREATE UNIQUE INDEX idx_entries_report_sku`
   statement that follows it — this ordering is load-bearing and correct: creating the unique
   index first would fail outright against any pre-existing database with duplicate
   `(report_id, sku)` rows (confirmed exactly this behavior is what
   `tests/test_stock_entries_uniqueness.py` exercises, and it passes).
3. `_dedupe_stock_entries` itself: correctly gated by an `EXISTS(... HAVING COUNT(*) > 1)` check
   (cheap no-op on any post-fix database), and when triggered, sums the same numeric columns
   `parser.py` now aggregates pre-insert — same rule applied retroactively, not a different one.
4. This is confirmed to be the entirety of the app's "migration framework" — inline idempotent
   schema-setup in `SCHEMA` (all `CREATE TABLE/INDEX IF NOT EXISTS`) plus this one explicit
   column-add/dedupe function for the one case where `IF NOT EXISTS` isn't sufficient (adding a
   NOT NULL column with a default to an existing table). No versioned migration file system
   exists, consistent with Agent 9's confirmation this is live, load-bearing infrastructure, not
   legacy cruft.

No correctness or ordering issues found. No changes made to this logic (per the hard constraint —
would be a data-affecting/behavioral change even to "clean it up").

## Unused-by-application-SQL index candidates (flagged only, NOT removed)
Three indexes exist in `SCHEMA` that no query anywhere in the codebase filters/joins/orders by —
verified by grepping every `WHERE`/`GROUP BY`/`ORDER BY`/`JOIN` clause in `db.py` (the only module
that issues raw SQL) and confirming all brand/SKU/product-name filtering actually happens in
pandas *after* an unconditional full-table load (`load_all_entries`, `list_item_catalog_names`,
`get_name_change_map` all load/scan the whole table with no `WHERE` on the indexed column):

1. **`idx_entries_sku` on `stock_entries(brand, sku)`** (`db.py:58`) — no query anywhere does
   `WHERE brand = ?` / `WHERE sku = ?` / joins on these columns. All SKU/brand search
   (`analysis.search_skus`, `sku_history`, `brand_history`) operates on the in-memory DataFrame
   from `load_all_entries()`, which has no `WHERE` clause at all.
2. **`idx_item_catalog_product` on `item_catalog(product_name)`** (`db.py:118`) — no query filters
   on `product_name`; the one place it's compared (`list_item_catalog_names`) does an unfiltered
   `UNION SELECT` and returns a Python `set`, with matching done in `analysis.py`, not SQL.
3. **`idx_item_name_changes_old` on `item_name_changes(old_name)`** (`db.py:134`) — same pattern:
   `get_name_change_map` does an unfiltered `SELECT ... ORDER BY rowid`, no `WHERE old_name = ?`
   anywhere.

**Not removed** — index removal is explicitly out of scope (data/schema-affecting) per this
agent's charter, and there's a plausible reason all three exist anyway: they read as forward-looking
indexes for query patterns the code doesn't use *yet* (e.g. a future "look up this SKU directly"
endpoint), not obviously accidental. `idx_entries_report` (on `stock_entries.report_id` alone) and
`idx_entries_report_sku` (the UNIQUE constraint index), by contrast, **are** actively used — the
correlated subqueries in `get_report`/`list_reports` (`WHERE e.report_id = r.id`) and the
FK-cascade delete path both benefit from `idx_entries_report`. Flagging the three above for a human
to decide whether to keep (as forward-looking) or drop (as genuine cruft) — this determination
requires knowing product intent, which is outside this pass's remit.

## Item-catalog write-only columns — already correctly documented, re-confirmed
`packing`, `mrp`, `by_rate`, `tax_pct`, `hsn` in `item_catalog` are captured on import but never
read back by any query (confirmed by grep — zero `SELECT`s of these column names anywhere except
inside `ITEM_CATALOG_COLUMNS`'s round-trip for catalog replace/rollback). This is already called
out explicitly in the schema's own comment (`db.py:100-104`) as deliberately-retained data for a
plausible near-future feature, not accidental cruft — re-confirmed this reasoning still holds
(no dead code review since has found a reason to doubt it) and left as-is, no new flag needed.

## Seed/fixture data vs. schema — no staleness found
Only one test (`tests/test_stock_entries_uniqueness.py`) hard-codes a `CREATE TABLE` schema
inline, and it deliberately encodes the **pre-migration** schema (no `value_tier_*` columns don't
even apply — it's `reports`/`stock_entries` only, intentionally omitting the not-yet-existing
unique index) to exercise the dedupe-then-index migration path. This is correct by design, not
stale — confirmed its own docstring states this purpose accurately. No other test file hard-codes
a schema; the rest all go through `db.connect()` (a temp DB path), which always runs the real,
current `SCHEMA` + `_migrate()`.

## External-consumer check (per task brief's instruction to verify, not assume)
Confirmed: no API/CLI output is documented or tested as a stable external contract. `main.py`'s CLI
output is human-readable text; `webapp.py`'s `/api/*` JSON endpoints are consumed only by this
app's own `templates/*.html` JS (fetch calls to same-origin paths, confirmed by grep — no
OpenAPI/swagger spec, no versioned API docs, no external client in the repo). This matches Agent
12's independently-reached conclusion (security audit) that this is a fully local, single-user app
with no external consumers. The "fields the app no longer reads but might be needed for external
consumption" concern from the task brief therefore doesn't apply here, as anticipated.

## Changes implemented
**None.** No schema drift, no incorrect migration ordering, no type-hint/SQL mismatch, no stale
fixture — nothing qualified as a low-risk purely-structural fix. Re-verified is green rather than
changed.

## Files touched
None (source unchanged). Only this handoff file is new.

## Validation (FULL tier, as assigned)
- `uv sync`: resolved cleanly, 24 packages (unchanged).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (unchanged from Agent 12's handoff).
- `ruff check .`: All checks passed.
- `ruff format --check .`: 41 files already formatted.

## Data-affecting items for human review (none implemented, all deferred)
| # | Item | Recommendation |
|---|---|---|
| 1 | `idx_entries_sku` (stock_entries.brand, sku) unused by any current SQL query | Decide: keep as forward-looking, or drop as cruft — human product-intent call |
| 2 | `idx_item_catalog_product` (item_catalog.product_name) unused by any current SQL query | Same as above |
| 3 | `idx_item_name_changes_old` (item_name_changes.old_name) unused by any current SQL query | Same as above |
| 4 | `item_catalog.packing/mrp/by_rate/tax_pct/hsn` write-only, no reader yet | Already documented in-schema as intentional; no new action needed, just re-confirmed |

No column/table/index was added, dropped, or altered. No migration logic was touched.

## Notes for Agent 14 (Documentation Sync Audit)
- The `db.py` module docstring and every function docstring read as accurate against the real
  schema/behavior — no doc-vs-code drift found in this file worth Agent 14 re-flagging.
- If Agent 14 finds README or other docs describing the DB schema, the ground truth to check
  against is `db.py:28-135` (`SCHEMA`) plus `_migrate()` (`db.py:138-160`) for the two ALTER-added
  `settings` columns and the `idx_entries_report_sku` unique index — those two migration-added
  pieces are easy for external docs to have missed if written before they existed.
- No behavior changed this pass, so no README/docs statement should have gone stale as a result of
  this agent's work specifically.
