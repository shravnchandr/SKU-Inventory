# Agent 9 — Legacy & Fallback Code Removal — Summary

## Method
Read `.cleanup/00-baseline.md` and `agent-1-summary.md` through `agent-8-summary.md` first.
Agent 7 already ran a dead-code sweep (vulture + ruff F401/F841 + manual reference-counting) and
found nothing removable. Agent 8 confirmed there are no fallback-on-error patterns anywhere.
This pass targets a different category — legacy/superseded *code paths* and *feature flags* —
so it re-searched from scratch rather than relying on those findings, per the task brief.

Searches run across `src/`, `app.py`, `main.py`, `tests/`, `README.md`, `pyproject.toml`:
1. `grep -rniE "TODO|FIXME|HACK|deprecated|legacy|temporary|migration|remove after|v1|v2|backward.?compat|old.?version|obsolete"`
2. `grep -rniE "os\.environ|os\.getenv|getenv\("` (env-var-gated branches / feature flags)
3. `grep -rn "if False|if True:|# type: ignore|NotImplementedError|pragma: no cover"`
4. `grep -rniE "compat|adapter|wrapper|shim"` (wrapper/adapter layer naming)
5. `grep -rniE "_old\b|_v1\b|_new\b|old_|alt_"` (dual-implementation naming)
6. `grep -n "environ|getenv|config\["` restricted to production source (double-check the
   env-var-flag assumption specifically, per task brief's instruction to verify rather than
   assume)
7. `git log --all --oneline | grep -niE "TODO|temporary|legacy|migration|deprecat"` (check
   history for anything flagged as transitional across the whole repo, not just current tree)

## Findings

### Text-match hits, all investigated and confirmed non-actionable
- `tests/test_xls_legacy_format.py` — "legacy" here means the older binary `.xls` Excel format
  (as opposed to `.xlsx`), which is a real, currently-supported input format read via
  `pandas.read_excel` (single code path handles both transparently, confirmed by reading
  `parser.py:1-90` — no separate xls-only/xlsx-only branch exists). Not deprecated, not a second
  path — it's the one and only path, tested for both extensions. No action.
- `tests/test_stock_entries_uniqueness.py` — tests `db.py`'s `_migrate`/`_dedupe_stock_entries`,
  which runs on every `connect()` call as the app's ongoing schema-init/idempotent-migration
  mechanism (ensures a unique index exists, deduping any pre-existing violations first). This is
  live, load-bearing infrastructure that runs on every DB connection, not a one-time vestige to
  delete — removing it would silently reintroduce duplicate-SKU corruption on any DB created
  before the index existed. No action.
- `june_v2.xlsx`, `"SOMETHING ELSE V2"` (tests) — literal fixture filenames/test data values,
  unrelated to code versioning. No action.
- Every `old_*` identifier (`old_report`, `old_name`, `old_product`, `old_period`, `OLD_BRAND`,
  etc.) is a business-domain variable naming an item's *previous* value (previous report,
  previous SKU name, previous brand name in a rename-tracking feature) — not a legacy/superseded
  code path. No action.

### No hits at all for
- Feature-flag-style conditionals: zero `os.environ`/`os.getenv` calls anywhere in production
  source (`src/`, `app.py`, `main.py`) — confirmed by direct grep, not assumed. The only
  `config[...]` usage found is Flask's `app.config` dict, used for per-app-instance state
  (`DB_PATH`, `UPLOADS_DIR`, `_SUMMARY_CACHE`, `CSRF_TOKEN`, `MAX_CONTENT_LENGTH`) — these are
  request-time app configuration values (DB path, cache slot, generated CSRF secret), not
  environment-driven behavioral toggles, and there is no branch anywhere that reads one of these
  to choose between two alternate implementations. **Confirmed (not assumed): this is a solo
  local app with zero runtime-toggle infrastructure of any kind** — no LaunchDarkly-style
  service, no env-var flags, no config-driven dual code paths.
- `if False`/`if True:`/commented-out disabled blocks/`NotImplementedError`/`pragma: no cover`.
- Adapter/wrapper/shim/compat-named modules, classes, or functions.
- Any commit in the full git history flagged with TODO/temporary/legacy/migration/deprecated
  language beyond what's already covered above.

## Changes implemented
**None.** No legacy code paths, dead feature flags, superseded implementations, or
wrapper/adapter layers exist in this codebase. Every "legacy"/"old"/"migration" text match is
either (a) a currently-supported input format with a single unified code path, (b) live
schema-maintenance infrastructure that runs on every connection, or (c) an ordinary
business-domain variable name unrelated to code versioning. This is consistent with Agent 7's
independent dead-code sweep (also found nothing) and Agent 8's finding that no fallback-on-error
patterns exist — three different lenses (dead code, error-handling fallbacks, legacy/flag code
paths) applied by three different agents all converge on the same conclusion: this codebase does
not carry legacy cruft.

## Files touched
None (source unchanged). Only this handoff file is new.

## Validation (FULL tier, as assigned)
Re-ran to reconfirm baseline health since no code changed:
- `uv sync`: resolves cleanly, 24 packages (unchanged).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (unchanged from Agent 8's handoff).
- `ruff check .`: All checks passed.
- `ruff format --check .`: 37 files already formatted.

## Recommendations NOT implemented
None — nothing actionable was found to defer.

## Notes for Agent 10 (Comment & Clarity Audit)
- No code was removed, so there are no dangling comments referencing deleted symbols to clean up.
- `tests/test_xls_legacy_format.py`'s docstring and `tests/test_stock_entries_uniqueness.py`'s
  module docstring both use "legacy" accurately (older Excel binary format; a database created
  before the unique index existed) — these are precise, not stale, and shouldn't be flagged as
  clarity issues.
- `ReportMeta.period_days`'s `return 122  # fallback: ~4 months` (parser.py) is a real fallback
  default for when period dates can't be parsed from the banner, not legacy code — worth Agent 10
  double-checking the comment still reads clearly, but it's out of scope for removal (it's an
  active default-value business rule, not a superseded path).
