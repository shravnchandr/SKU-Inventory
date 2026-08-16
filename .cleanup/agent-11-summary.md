# Agent 11 — Configuration & Secrets Hygiene — Summary

## Secrets audit result: NONE FOUND (state this explicitly, as required)
- Searched every git-tracked file (`.py`, `.command`, `.bat`, `.toml`, `.cfg`, `.ini`, `.html`) for
  API-key/secret/password/token/bearer patterns and common credential formats (`AKIA...`,
  `-----BEGIN`, `sk-...`). No matches besides the app's own CSRF-token *mechanism* (not a secret
  value, a per-process random token generated at runtime via `secrets.token_hex(16)` in
  `webapp.py:141` and never persisted or committed — this is the correct pattern, not a finding).
- Searched full git history (`git log -p --all`) for the same patterns. No matches.
- No `.env` file, no credentials file, nothing resembling a secret anywhere in the working tree or
  history. Confirms Agent 10's note that this is a solo local Flask app with no env-var-driven
  config, and extends it: there is also no secret of any kind checked in, past or present.
- **No action needed under the Secrets Protocol** — nothing to remove, nothing to flag as
  compromised, nothing for history-scrubbing tooling to address.

## Configuration surface audited
Read `app.py`, `main.py`, and all of `src/gowri_proj/*.py` (`analysis.py`, `dashboard.py`, `db.py`,
`excel_export.py`, `parser.py`, `sync.py`, `webapp.py`), plus `install.command`/`install.bat`/
`run.command`/`run.bat`/`update.command`/`update.bat`, `pyproject.toml`, `.gitignore`.

### Findings
1. **`DEFAULT_UPLOADS_DIR = "uploads"` duplicated with no single source of truth** (LOW severity,
   fixed). Independently defined as a literal in both `main.py` (line 41) and
   `src/gowri_proj/webapp.py` (line 51) — the exact same value, no link between them, unlike the
   sibling constant `DEFAULT_DB_PATH`, which was already correctly defined once in `db.py` and
   imported everywhere else (`webapp.py`'s `DEFAULT_DB_PATH = db.DEFAULT_DB_PATH`, `main.py`'s
   `db.DEFAULT_DB_PATH` used directly as an argparse default). A latent drift risk of the same
   shape Agent 6 fixed for `dashboard.DEFAULT_THRESHOLDS` vs. `db.DEFAULT_SETTINGS`. **Fixed**:
   moved `DEFAULT_UPLOADS_DIR` to live in `src/gowri_proj/sync.py` (the module that owns
   folder-scanning behavior, `sync_folder`/`fy_folder`), imported from there by both `main.py` and
   `webapp.py`. Pure mechanical relocate — value unchanged (`"uploads"` in both places, before and
   after), no runtime behavior change, no new load order or failure mode.
2. **All other "config" is already well-factored** — no further action taken:
   - `app.py`: `HOST = "127.0.0.1"` / `PORT = 8765` are already named module-level constants, used
     consistently for binding, health-check, and browser-open. No duplication elsewhere.
   - `webapp.py`: `app.config["DB_PATH"]`, `["UPLOADS_DIR"]`, `["MAX_CONTENT_LENGTH"]`,
     `["CSRF_TOKEN"]`, `["_SUMMARY_CACHE"]` are Flask's own config dict, populated once in
     `create_app()` from named module constants (`DEFAULT_DB_PATH`, `DEFAULT_UPLOADS_DIR` (now
     imported, see above), `MAX_UPLOAD_BYTES`) or generated at process start (`CSRF_TOKEN`). No
     inconsistency.
   - `db.py`'s `DEFAULT_SETTINGS` and `analysis.py`'s threshold/tier constants
     (`LOW_STOCK_DAYS`, `OVERSTOCK_DAYS`, `TRAILING_DAYS_TARGET`, `DEAD_STOCK_DAYS`,
     `VALUE_TIER_A_PCT`, `VALUE_TIER_B_PCT`, `THRESHOLD_DAYS_MIN/MAX`, `THRESHOLD_PCT_MIN/MAX`,
     `AGING_LOOKBACK_FLOOR_DAYS`, `_VALUE_NOISE_FLOOR`, `_STOCK_BALANCE_TOLERANCE`) — already
     centralized by Agent 6's pass (`dashboard.DEFAULT_THRESHOLDS` now derives from these instead
     of re-hardcoding). Confirmed no stragglers: grepped every module for bare numeric/string
     literals that looked like they should be named constants; the remainder (parser.py's
     `COL_*`/`IL_COL_*` column-index constants, `db.py`'s `_STOCK_ENTRY_NUMERIC_COLS`,
     `ITEM_CATALOG_COLUMNS`, `sync.SUPPORTED_SUFFIXES`) are already named, single-defined, and
     domain constants rather than environment config.
   - No `if debug`/`if env == "production"`-style branches anywhere — `app.run(..., debug=False,
     use_reloader=False)` in `app.py` is a fixed, intentional value (this is a hand-launched local
     app, not something toggled by environment), not a hidden environment-specific branch.
   - `install.command`/`install.bat`/`run.command`/`run.bat`/`update.command`/`update.bat`:
     read for hardcoded URLs/hosts/credentials. Found only the public, non-secret
     `https://astral.sh/uv/install.sh` / `install.ps1` URLs (uv's own official installer, the
     standard documented way to bootstrap uv) and `BRANCH="main"` — both appropriate as literals
     for a small deploy script, not config-system candidates.
   - `pyproject.toml`: single source of truth for dependencies/tool config already, no
     duplication against any other file.
   - No env-var-driven config exists (`os.environ`/`os.getenv` absent, reconfirmed) — nothing to
     assess for "changes WHEN/HOW a value is read at runtime", so this pass stayed at LIGHT tier
     throughout (the one change made is a pure import-relocate of an unchanged string literal).

## Changes implemented
- `src/gowri_proj/sync.py`: added `DEFAULT_UPLOADS_DIR = "uploads"` (+1 line).
- `src/gowri_proj/webapp.py`: import `DEFAULT_UPLOADS_DIR` from `.sync` instead of redefining it
  (+1/-2 net line change on the import line plus removed the local definition).
- `main.py`: import `DEFAULT_UPLOADS_DIR` from `src.gowri_proj.sync` instead of redefining it
  (net -3 lines: removed local `DEFAULT_UPLOADS_DIR = "uploads"` definition and its blank-line
  spacing, folded into the existing import line).

`git diff --stat`: `main.py` (4 changed), `src/gowri_proj/sync.py` (+1), `src/gowri_proj/webapp.py`
(3 changed) — 3 files, +3/-5 lines total. No behavior change: same string value, same effective
default, now with one source of truth instead of two.

## Validation
**LIGHT tier** (as assigned — the one change is a pure mechanical relocate, no runtime
load-order/failure-mode change, so no upgrade to FULL was warranted):
- `uv sync`: resolves cleanly, 24 packages (unchanged).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (unchanged from Agent 10's handoff state).
- `ruff check .`: All checks passed.
- `ruff format --check .`: 39 files already formatted.

All green — no fix attempts needed.

## Recommendations NOT implemented
None beyond what's already been addressed — the codebase's configuration surface is small and
already well-organized for what it is (a single-user local Flask app launched by double-clickable
scripts). No config system, `.env` support, or environment-variable layer is warranted or was
introduced, per the hard constraint.

## Notes for Agent 12 (Security Audit)
- **No secrets, credentials, or API keys exist anywhere in the codebase or git history** —
  confirmed by full-history grep, not just working-tree. Agent 12 can treat this surface as clean
  and focus elsewhere.
- CSRF protection (`webapp.py:136-141, 218-220`) uses a per-process random token
  (`secrets.token_hex(16)`, compared with `secrets.compare_digest`) generated fresh on every
  `create_app()` call and never persisted — this is a deliberate design already documented in an
  inline comment (see Agent 10's summary) for why it's not stored. Worth Agent 12 confirming this
  is applied to all mutating routes, but the mechanism itself is sound and out of scope for this
  (config/secrets) pass.
- `app.run(host="127.0.0.1", ...)` binds to localhost only, by design (see `webapp.py`'s module
  docstring: "Runs entirely on localhost — no data leaves the machine"). Not a config issue; noting
  for Agent 12 in case network-exposure is part of their checklist.
- `MAX_UPLOAD_BYTES = 50 * 1024 * 1024` (50MB) in `webapp.py` is the only upload-size guard;
  confirmed it's wired into `app.config["MAX_CONTENT_LENGTH"]` so Flask enforces it at the
  framework level, not just app-level logic — relevant if Agent 12 is checking upload-handling
  hardening.
