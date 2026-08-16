# Agent 6 — Type Consolidation & Strengthening — Summary

## Method
Read `.cleanup/00-baseline.md` and `agent-1-summary.md` through `agent-5-summary.md` first
(agent-5's confirmed module graph: `analysis.py`/`parser.py` are leaves; `db.py`/`dashboard.py`/
`excel_export.py` depend only on those two; `sync.py` adds `db`; `webapp.py` depends on
everything; `main.py`/`app.py` are entry points — used this to decide where shared types belong).

Then: found every type definition (`grep` for `class `, `@dataclass`, `NamedTuple`, `TypedDict`,
`Enum`, `Protocol` across all 10 Python files), compared shapes for duplication, then swept every
`-> dict`, `-> list`, `: dict`, `: list`, and bare/missing annotation across the codebase to find
weak-typing candidates, verifying each against real call sites/data shapes before touching it.

**Correction to the task brief's assumption**: this codebase is *not* "few/no type hints" — it's
already substantially typed. Every function in `parser.py`, `sync.py`, `excel_export.py`, and
nearly all of `db.py` already has full parameter and return annotations (confirmed by reading
every `def` in those files). The real gap was narrower: a handful of bare `dict`/`list` return
types standing in for well-defined, and in several cases *cross-module-shared*, shapes, plus a
few untyped Flask-adjacent parameters with a verifiably single, correct type. Prioritized
accordingly rather than blanket-annotating.

## Part A — Consolidation

### Existing dataclasses surveyed (no merge needed — genuinely different concepts)
`ReportMeta`/`ItemListMeta` (parser.py), `SyncResult` (sync.py), `ImportResult` (db.py),
`HistoryMeta`/`TrendSeries`/`InventorySummary` (analysis.py). `ReportMeta` and `ItemListMeta`
both carry `company`/`location` but diverge on the rest (`period_start`/`period_end` vs. `as_of`)
— Agent 2 already looked at this exact pair and correctly declined to merge it; re-confirmed the
same conclusion independently. No dataclass/type duplication found among these.

### Real duplication found and fixed: `dashboard.DEFAULT_THRESHOLDS` vs `db.DEFAULT_SETTINGS`
Both dicts encode the same six default threshold values (`low_stock_days=15`,
`overstock_days=90`, `trailing_days_target=90`, `dead_stock_days=90`, `value_tier_a_pct=70`,
`value_tier_b_pct=90`). `db.DEFAULT_SETTINGS` already referenced `analysis.LOW_STOCK_DAYS` etc.
(the single source of truth); `dashboard.DEFAULT_THRESHOLDS` had silently re-hardcoded the same
six numbers as *literals*, with no link back to the constants — a latent drift bug (if a
constant in `analysis.py` ever changed, dashboard's Flask-page-never-hit fallback would go stale
without anyone noticing, since no test exercises it). Fixed by having `dashboard.py` build
`DEFAULT_THRESHOLDS` from the same `analysis.py` constants `db.py` uses. Verified this fallback
is never actually reached by any current call site (`main.py`/`webapp.py` always pass explicit
thresholds from `db.get_settings`) — so this is a latent-bug fix, not an observed-behavior change,
and definitely not a value change (`15/90/90/90/70/90` in both before and after).

### New shared types (added to `analysis.py` — the correct home per Agent 5's graph: every other
internal module already depends on it, and adding a type here can't create the one cycle that
would matter, i.e. `analysis` depending back on `db`/`dashboard`/`webapp`)
- `ThresholdValues` (TypedDict) — the six user-configurable status thresholds. What
  `dashboard.py`'s `_status_blurbs`/`build_payload`/`render_html`/`write_dashboard` actually need.
- `Settings(ThresholdValues)` (TypedDict, structural extension) — adds `version`/`updated_at`,
  the full shape of a `settings` DB row as returned by `db.get_settings`/`db.upsert_settings`.
  Previously both were annotated as bare `dict` in `db.py`, `dashboard.py`, and `main.py`
  despite being the exact same shape passed around by all three.
- `CoverageGap` / `ImportGaps` (TypedDict) — `analysis.find_import_gaps`'s return shape (was
  bare `dict`), and the per-gap dict inside it. Now also used for `HistoryMeta.trailing_window_gaps`
  (was bare `list`).
- `DeadStockAgingBucket` — `analysis._dead_stock_aging`'s return shape (was bare `list`); now also
  types `InventorySummary.dead_stock_aging` (was bare `list`).
- `ValueSegmentSummary` — `analysis._value_segment_summary`'s return shape (was bare `list`); now
  also types `InventorySummary.value_segments` (was bare `list`).

### New local type (added to `db.py` — used only there/at one call site, so kept local rather
than promoted to `analysis.py`)
- `CatalogMeta` (TypedDict) — `db.get_item_catalog_meta`'s return shape (was bare `dict | None`).

## Part B — Strengthening
- `db.connect()` — added `-> Iterator[sqlite3.Connection]` (it's `@contextmanager`-decorated and
  yields a `sqlite3.Connection`; was entirely unannotated).
- `main._resolve_thresholds(conn, args)` — `conn` was untyped; added `sqlite3.Connection`
  (verified every call site passes the `conn` from `db.connect(...)`), and changed the return
  type from bare `dict` to the new `Settings`.
- `webapp._value_segment_for(summary, sku)` — `summary` was untyped; verified its one call site
  passes the `InventorySummary` from `get_current_data()` (narrowed non-`None`) — added the type.
- `webapp._serialize_reports(reports_df)` — was untyped; verified it's always
  `db.list_reports()`'s `pd.DataFrame` output — added `pd.DataFrame` (added `import pandas as pd`
  to webapp.py, previously not imported there).
- `webapp._validate_upload(file)` / `_staged_upload(file, ...)` — `file` was untyped; verified
  both call sites pass `request.files.get("file")`, i.e. `werkzeug.datastructures.FileStorage |
  None` for the first (checked internally before use) and a confirmed-present `FileStorage` for
  the second — added both (`from werkzeug.datastructures import FileStorage`).

## Not implemented (documented, not high-confidence enough or too broad a diff)
- **Flask route handler bodies in `webapp.py`** (`index`, `dashboard`, `trends`, `api_search`,
  etc.) — left untyped. Flask's view-function return type is a union of `str | Response |
  tuple[Response, int] | ...` (`werkzeug.wrappers.Response` / `flask.typing.ResponseReturnValue`)
  that varies per-route and isn't a single verifiable type without deeper Flask-typing knowledge
  this pass didn't want to guess at; annotating ~20 route closures for uncertain payoff would also
  blow past the "reviewable diff" bar. Left as a recommendation, not implemented.
- **`analysis.py`'s other `list[dict]` returns** (`search_skus`, `sku_history`, `brand_history`,
  `find_data_quality_issues`, `find_sku_churn`, `find_unmatched_skus`) — each has a distinct,
  well-defined dict shape, but each is produced and consumed in exactly one place (a single Flask
  JSON endpoint), not shared across modules — lower payoff than the cross-module shapes actually
  implemented above. Could be TypedDict'd in a follow-up if the team wants full annotation
  coverage; listed here rather than done, to keep this pass's diff reviewable.
- **`dashboard.py`'s internal `dict`/`list[dict]` params** (`build_payload`'s return `-> dict`,
  `quality_issues: list[dict]`) — same reasoning: correct but low-leverage, single-produce
  single-consume shapes; not touched.
- **mypy/pyright as a configured gate**: NOT recommending this be added. The codebase's types are
  now meaningfully stronger at the boundaries that matter (shared config/DB-row shapes), but full
  strict-mode compliance would require touching every Flask route handler, every `pd.DataFrame`
  column-access pattern (which mypy/pandas-stubs handle poorly without heavy `# type: ignore`
  noise), and would be a much bigger policy decision than this pass's scope. If a human wants to
  pursue it, `pyright` in basic (not strict) mode would likely be the lower-friction starting
  point given how much of `analysis.py`/`db.py` is already typed — but that's a recommendation,
  not something this pass implements or gates on, per the task brief.

## Files touched (rough +/- from `git diff --stat`)
- `src/gowri_proj/analysis.py` (+76/-4): new TypedDicts (`ThresholdValues`, `Settings`,
  `CoverageGap`, `ImportGaps`, `DeadStockAgingBucket`, `ValueSegmentSummary`), applied to
  `HistoryMeta`/`InventorySummary` fields and `_dead_stock_aging`/`_value_segment_summary`/
  `find_import_gaps` return types.
- `src/gowri_proj/db.py` (+16/-3): `Settings`/`CatalogMeta` TypedDict usage on `get_settings`/
  `upsert_settings`/`get_item_catalog_meta`; `connect()` return type.
- `src/gowri_proj/dashboard.py` (+35/-11): import + use `ThresholdValues`; deduplicated
  `DEFAULT_THRESHOLDS` against `analysis.py`'s constants; typed `thresholds` params across
  `_status_blurbs`/`build_payload`/`render_html`/`write_dashboard`.
- `src/gowri_proj/webapp.py` (+11/-4): typed `_value_segment_for`, `_serialize_reports`,
  `_validate_upload`, `_staged_upload`; added `pandas`/`werkzeug.datastructures.FileStorage`
  imports.
- `main.py` (+4/-1): `Settings` import, typed `_resolve_thresholds`'s `conn` param and return.

No files created, no files deleted, no test files touched, no runtime dependency changes.

## Validation (FULL tier, as assigned)
- `uv sync`: resolves cleanly, 24 packages (unchanged).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed — same count as Agent 5's handoff state.
- `ruff check .`: All checks passed (one `UP035` auto-fix applied during the pass: `Iterator`
  import moved from `typing` to `collections.abc` per ruff's preference).
- `ruff format --check .`: 34 files already formatted.

No mypy/pyright run as part of the gate (none configured, per baseline — matches the task's
instruction not to add one). Did spot-check a few of the new TypedDict usages by eye against
real call sites instead (no ad hoc mypy/pyright run was needed to reach high confidence here,
since each TypedDict was derived directly from an existing dict literal already in the code).

## Notes for Agent 7 (Dead Code Removal)
- No functions, classes, or code paths were removed or added in this pass — purely
  type-annotation and one small dict-construction change (`dashboard.DEFAULT_THRESHOLDS` now
  built from constants instead of literals, same resulting values).
- Module dependency graph is **unchanged** from Agent 5's confirmed layout — no new imports
  were added that cross the established layering (`dashboard.py` now imports more *names* from
  `.analysis`, which it already depended on; no new module-level edge was introduced anywhere).
- If Agent 7 finds `dashboard.DEFAULT_THRESHOLDS`'s fallback branch (`thresholds = thresholds or
  DEFAULT_THRESHOLDS` in `build_payload`) looks unreachable in practice (confirmed above: every
  real caller passes explicit thresholds) — that's a `None`-safety fallback for API robustness,
  not dead code to remove; flagging here so it isn't miscategorized.
- The six new TypedDicts in `analysis.py` are pure static-typing constructs (erased at runtime,
  `TypedDict` from `typing`, zero runtime behavior) — nothing for a dead-code pass to worry about
  removing/keeping based on runtime reachability.
