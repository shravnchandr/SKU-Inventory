# Agent 12 — Security Audit (report-only) — Summary

## Overall result
**No HIGH or CRITICAL findings.** Ran LIGHT validation tier (no upgrade triggered). Findings below
are informational/LOW, plus context notes on deliberate design choices that are not findings.
No code changes made — this pass is report-only per its charter, and nothing qualified as a
purely-structural, zero-behavior-change fix.

## Scope covered
`app.py`, `main.py`, all of `src/gowri_proj/*.py` (`db.py`, `webapp.py`, `sync.py`, `parser.py`,
`analysis.py`, `dashboard.py`, `excel_export.py`), all four Jinja templates
(`src/gowri_proj/templates/{base,dashboard,trends,reports,settings}.html`), and the deploy scripts
(`install.command`, `install.bat`, `run.command`, `run.bat`, `update.command`, `update.bat`).

## Findings

### 1. (INFORMATIONAL / no action) `db.py:597` uses an f-string in a SQL query
`get_item_catalog_df`: `pd.read_sql(f"SELECT {', '.join(ITEM_CATALOG_COLUMNS)} FROM item_catalog", conn)`.
This interpolates `ITEM_CATALOG_COLUMNS`, a fixed module-level constant list of column names
(`code, brand, product_name, ...`) — never user input, never runtime-derived. **Not exploitable**:
there is no code path where user-controlled data reaches this f-string. Flagging only because
f-string-built SQL is a pattern worth a human's eye if the codebase ever grows a second caller that
passes a variable column list; every other query in `db.py` (all ~20 of them) is fully parameterized
with `?` placeholders and a tuple of bound values, including every place actual request data
(report ids, filenames, thresholds) reaches SQL. No fix recommended — changing this would be a
no-op refactor, not a security fix, and outside "zero behavioral surface" scope for a f-string
literally built from a constant.

### 2. (LOW) No rate limiting / brute-force protection on `/api/*` routes
All mutating routes are protected by the CSRF token check (`webapp.py:212-219`), but there's no
rate limiting on repeated requests. Given the app binds to `127.0.0.1` only (see below) and the
CSRF token is unguessable (`secrets.token_hex(16)` = 128 bits of entropy, compared with
`secrets.compare_digest`), the practical attack surface is near-zero: an attacker would need
either local code execution (at which point rate limiting is moot) or to guess/exfiltrate the
in-memory token from another origin, which the CSRF mechanism specifically exists to prevent.
**Recommended fix (not implemented — behavioral change)**: none needed given the localhost-only
threat model; would only matter if this app's binding assumption changes in the future (see
Recommendation section below).

### 3. (LOW, documentation-only, not a defect) No authentication on any route
Every `/dashboard`, `/reports`, `/settings`, `/api/*` route is reachable by any request that
reaches the Flask process, with no login/session/user concept anywhere in the codebase (confirmed:
zero uses of `flask.session`, no `SESSION_COOKIE_*` config, no password/auth code of any kind).
**This is a deliberate design choice, not an oversight**, consistent with Agent 10's and Agent 11's
notes: `app.py` binds `host="127.0.0.1"` (verified directly at `app.py:16` and the `app.run(host=HOST, ...)`
call at `app.py:39`) — genuinely localhost-only, not just documented as such — and the app's own
module docstring (`webapp.py:3`) states "Runs entirely on localhost — no data leaves the machine."
For a single-user, hand-launched local app with no network exposure, an auth layer would add
complexity without a corresponding threat it defends against (the CSRF token already covers the one
real cross-origin risk: another tab/site in the same browser issuing requests to this port). Noting
for the record per the task's own guidance to document rather than flag this as a finding — but
**recommend this note be revisited if the app's distribution/deployment model ever changes** (e.g.
if a future version binds to `0.0.0.0` for multi-machine access, or is deployed behind a reverse
proxy) — at that point "no auth" would become a real HIGH-severity gap.

### 4. (LOW) `secure_filename()` used correctly, but filename collision is possible after sanitization
`webapp.py:79`, `_validate_upload`: `filename = secure_filename(file.filename)` strips path
separators and dangerous characters — correct, standard defense against path traversal on upload.
Two different original filenames could sanitize to the same string (e.g. unicode homoglyphs or
control characters stripped to the same ASCII result), causing a silent overwrite of one upload's
staged temp file by another's. In practice this is low-severity: the destination path additionally
incorporates the parsed report period (`fy_folder(meta.period_start)/filename`) and import
correctness is independently validated by `period_exists`/`find_reused_filename_conflict`/
`find_superseded_reports` before the file is ever moved into place (see `webapp.py:475-506`), so a
sanitization collision at most causes a confusing "period already imported" error rather than data
corruption. No fix recommended — the existing period-based conflict detection is already the real
safety net here, not the filename itself.

### 5. (LOW) CSRF token is per-process, not per-session — acceptable for this app's model
Confirmed (per Agent 11's note and independently verified at `webapp.py:140` and `212-219`): the
CSRF token is generated once via `secrets.token_hex(16)` at `create_app()` time and checked via
`secrets.compare_digest` against the `X-CSRF-Token` header on every `POST`/`PUT`/`PATCH`/`DELETE`
under `/api/`. Confirmed this before-request hook fires unconditionally for **all** `/api/*`
mutating routes — checked every route decorated `@app.post(...)` (`api_save_settings`,
`api_remove_report`, `api_upload`, `api_upload_item_list`, `api_refresh`) and none bypass it (no
`view_func`-level exemption, no blueprint carve-out). This is sound for a single-process,
single-user local server; flagging only that if the app ever supports concurrent windows/tabs
across separate app restarts (it currently doesn't — token dies with the process), any long-lived
open tab from a prior process would fail CSRF checks against a newly restarted process's fresh
token, which is a UX footgun rather than a security gap (fails closed, not open).

### 6. (LOW / informational) Update mechanism (`update.command`/`update.bat`) does `git reset --hard
origin/main` over an unauthenticated `git fetch`
`update.command:31` / equivalent in `update.bat`: fetches from `origin` (whatever remote URL the
deployed copy's `.git/config` points at — normally the project's real GitHub remote, set up at
install time) and hard-resets the local working tree to it, with no signature/checksum verification
beyond git's own transport-level integrity (HTTPS + git object hashing). This is standard behavior
for a simple "pull to update" script and matches how e.g. `git pull` behaves by default — flagging
only as the sort of supply-chain link a from-source auto-update mechanism always has: whoever
controls the `origin` remote controls what code runs on the user's next update. No fix recommended;
this is inherent to the "double-click to update from git" design (documented, not hidden) and
changing it (e.g. adding commit signing verification) would be a significant behavior/tooling
change outside a report-only pass's remit.

### No findings in these areas (checked, clean)
- **SQL injection**: every query in `db.py` that touches user/request-derived data (report ids,
  filenames, threshold values, SKU/brand search strings via `request.args`) uses parameterized `?`
  placeholders with bound tuples — confirmed by reading all ~20 queries in `db.py` end to end. The
  one f-string-built query (finding #1 above) only interpolates a fixed constant list, never
  reachable user input.
- **Command injection**: no `subprocess`, `os.system`, `os.popen`, or `shell=True` anywhere in
  `app.py`, `main.py`, or `src/gowri_proj/*.py` (grepped, zero matches). The `.command`/`.bat`
  scripts run fixed, hardcoded command sequences (`uv sync`, `git fetch`, `git reset --hard
  origin/$BRANCH` where `BRANCH="main"` is a literal) with no interpolation of external/user input
  into a shell string.
- **Path traversal**: upload filenames go through `secure_filename()` before touching the
  filesystem (`webapp.py:79`); the only other filesystem write paths (`sync.py`'s folder scan,
  `db.py`'s `DEFAULT_DB_PATH`, dashboard/excel output paths) are driven by CLI args or hardcoded
  constants set by the app's own operator, not remote/attacker input.
- **Template injection / XSS**: no `|safe` filter, no `Markup(...)`, no autoescape-disabling
  anywhere in any of the four templates (grepped). The one place a full data payload is embedded
  into a page (`templates/dashboard.html:113`, `{{ payload | tojson }}` inside a `<script
  type="application/json">` tag) uses Jinja's `tojson` filter, which is the correct,
  escaping-safe pattern for this — Flask's `tojson` (via `htmlsafe_json_dumps`) escapes `<`, `>`,
  `&`, and `'` specifically so embedding inside `<script>` can't be broken out of. All other
  template variables (`{{ url_for(...) }}`, `{{ company }}`, etc.) go through Jinja's default
  autoescaping (Flask enables `autoescape=True` for `.html` templates by default; nothing in this
  app overrides that).
- **Deserialization**: no `pickle`, `eval`, `exec`, `yaml.load` (unsafe form) anywhere in the
  codebase (grepped, zero matches). File parsing (`parser.py`) uses `pandas`/`openpyxl`/`xlrd` to
  read `.xls`/`.xlsx` — these are binary-format parsers, not general deserializers of executable
  code (no macro/formula execution: `openpyxl` and `xlrd` here are read-only and don't evaluate
  formulas or run embedded macros by default, and this app never opts into that).
- **Insecure crypto**: no password/auth code exists at all (see finding #3), so there's no password
  hashing to critique. The only crypto-adjacent code is CSRF-token generation
  (`secrets.token_hex(16)`, a CSPRNG — correct choice) and comparison (`secrets.compare_digest`, a
  constant-time comparison — correct choice, avoids timing side-channels). No hardcoded salts/IVs,
  no MD5/SHA1-for-security-purposes, no deprecated algorithms anywhere.
- **CORS**: no CORS headers, no `flask-cors`, no `Access-Control-*` anywhere — the app never
  intends cross-origin API access (by design, single-origin localhost app), and absence of CORS
  headers is the more restrictive (safer) default, not a gap.
- **Cookie/session config**: no `flask.session` usage anywhere, no cookies set at all — nothing to
  configure (`SESSION_COOKIE_SECURE` etc. are moot with zero cookies in play).
- **Log injection**: the app uses `print()` for CLI output, not a structured logger with
  injectable format strings; nothing writes user-controlled strings into a log file that's later
  parsed/replayed.

## Validation
**LIGHT tier** (no HIGH/CRITICAL finding, so no upgrade to FULL — per the assignment's own rule).
No code changes were made (report-only pass, no purely-structural fix identified), so this run
exists to confirm the working tree is still green before commit, not to validate a change:
- `uv sync`: resolved cleanly, 24 packages (unchanged from Agent 11's handoff).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (unchanged).
- `ruff check .`: All checks passed.
- `ruff format --check .`: 40 files already formatted.

## Notes for Agent 13 (Database & Persistence Audit)
- All SQL in `db.py` is parameterized except one f-string that only interpolates a fixed constant
  column-name list (`get_item_catalog_df`, `db.py:597`) — not a security issue, but worth Agent 13
  independently judging on hygiene/consistency grounds since it's the one query in the file not
  shaped like its ~20 siblings.
- Schema/migration hygiene (the `_migrate()` function, `ALTER TABLE` guards, the `_dedupe_stock_entries`
  backstop) looked structurally sound on a read-through but wasn't this agent's focus — flagging
  that Agent 13 should give `db.py:138-194` a full pass, since it's doing real migration logic
  (column-existence checks before `ALTER TABLE`, a one-time dedupe pass gated on `_migrate` running
  before a new `CREATE UNIQUE INDEX`).
- `db.connect()` (`db.py:197-209`) opens a **new SQLite connection per call site** (every route
  handler, every CLI command does its own `with db.connect(...) as conn:`) rather than holding one
  connection across a request/process lifetime — not a security concern, but a persistence-layer
  pattern worth Agent 13's judgment on (connection-pooling/overhead trade-offs are out of scope for
  a security pass).
- No security-relevant data (PII, credentials) is stored in the DB — it's pharmacy inventory
  (SKUs, brand names, stock counts, values) — so no encryption-at-rest gap to note.

## Recommendations summary (all deferred to human review, none implemented)
| # | Finding | Severity | Action needed |
|---|---|---|---|
| 1 | f-string SQL in `get_item_catalog_df` (constant input only) | Informational | None — safe as-is, noted for hygiene |
| 2 | No rate limiting on `/api/*` | LOW | None needed under current localhost-only threat model |
| 3 | No authentication on any route | LOW (documented design) | Revisit only if binding/deployment model changes |
| 4 | `secure_filename()` collision theoretically possible | LOW | None — period-based conflict detection is the real safety net |
| 5 | CSRF token is per-process, not persisted | LOW | None — sound for single-process local app |
| 6 | Auto-update trusts `origin` remote unconditionally | LOW/informational | None — inherent to the update-from-git design |

No HIGH or CRITICAL findings. No code changes made this pass.
