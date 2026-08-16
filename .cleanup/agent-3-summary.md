# Agent 3 — Dependency Audit — Summary

## Method
Read `.cleanup/00-baseline.md`, `agent-1-summary.md`, `agent-2-summary.md`. Grepped every
import across `app.py`, `main.py`, `src/gowri_proj/*.py`, `tests/*.py`; cross-referenced against
`pyproject.toml` and `uv.lock`'s full resolved graph (23 packages: 5 direct-declared pre-change,
18 transitive).

## Per-dependency assessment

### Direct (declared)

- **flask (3.1.3)** — **Keep.** Full Flask app in `webapp.py` (599 lines: routes, `jsonify`,
  `render_template`, `redirect`, `url_for`, `abort`, `g`). Core to the product (it's a web
  dashboard). No overlapping HTTP framework in the graph. Current, actively maintained version.
- **pandas (3.0.5)** — **Keep.** Used broadly and non-trivially across `parser.py` (Excel
  ingestion, date parsing via `pd.to_datetime`, numeric coercion), `db.py` (`pd.read_sql` for
  every DB→DataFrame boundary), `analysis.py` (the entire analytics layer: `pd.Series`,
  `pd.DataFrame`, `pd.Timedelta`, `pd.period_range`), `dashboard.py`, `excel_export.py`
  (`pd.ExcelWriter`). This is not a "one function" import — replacing it with stdlib `csv`/
  `sqlite3` alone would be a large, high-risk rewrite of the analytics core, not a mechanical
  swap. No overlapping DataFrame library present. Version `3.0.5` verified real/installed
  (pandas 3.x line exists; not a typo/phantom version).
- **openpyxl (3.1.5)** — **Keep.** Two roles, both load-bearing: (1) pandas' engine for both
  reading `.xlsx` uploads (`pd.read_excel` auto-dispatches to it for `.xlsx`) and writing
  `.xlsx` exports (`pd.ExcelWriter(..., engine="openpyxl")` in `excel_export.py`); (2) directly
  imported in 3 test files (`test_stock_statement_duplicate_skus.py`,
  `test_webapp_catalog_upload.py`, `test_item_catalog.py`) to construct `.xlsx` fixtures via
  `openpyxl.Workbook()`. No unused-import case; no lighter drop-in replacement that preserves
  pandas' `.xlsx` write path.
- **xlrd (2.0.2)** — **Keep, with a note.** Never imported by name anywhere in the codebase —
  initially looked like a candidate for removal. Verified it is not dead weight: pandas'
  `read_excel` engine dispatch requires `xlrd` specifically to read legacy `.xls` files (it
  cannot use `openpyxl` for `.xls`, only `.xlsx`). `.xls` is the **primary documented input
  format** — README: `"Stock Statement" .xls exports your pharmacy software produces`,
  `SUPPORTED_SUFFIXES = {".xls", ".xlsx"}` in `sync.py`, and the upload validator in
  `webapp.py` explicitly accepts `.xls`. No test file currently exercises actual `.xls` parsing
  (all fixtures in tests use `openpyxl.Workbook()` → `.xlsx`), so this dependency's behavior is
  currently **not covered by the test suite** — flagged for Agent 4 (Test Quality Audit) below,
  not removed, since removing it would silently break real-world `.xls` imports with no test to
  catch it.

### Transitive, but directly imported (undeclared — fixed this pass)

- **numpy (2.5.1)** — **Finding: was undeclared as a direct dependency despite being directly
  imported.** `src/gowri_proj/analysis.py` has `import numpy as np` and uses `np.select` /
  `np.where` directly (vectorized status/tier computation) — this is a first-party import, not
  something pandas does internally on the app's behalf. It only worked because pandas pulls in
  numpy transitively. Per PEP 508 / packaging best practice, anything imported directly should
  be declared directly — an unpinned transitive dependency can be dropped or version-shifted by
  an upstream package (e.g., a future pandas release changing its numpy floor) without
  `pyproject.toml` reflecting the real constraint, silently breaking `analysis.py`.
  **Implemented:** added `"numpy>=2.5.1"` to `[project.dependencies]` in `pyproject.toml`,
  matching the version already resolved transitively — `uv sync` confirms no version change,
  same 23-package graph, just makes the existing resolution explicit.

### Transitive only (not directly imported by app code — correctly left undeclared)

`blinker`, `click`, `colorama`, `et-xmlfile`, `iniconfig`, `itsdangerous`, `jinja2`,
`markupsafe`, `packaging`, `pluggy`, `pygments`, `python-dateutil`, `six`, `tzdata`, `werkzeug`
— all pulled in by flask/pandas/pytest and used only through those libraries' own internals
(e.g. Jinja2 templating via `render_template`, Werkzeug via Flask's WSGI layer). None are
imported by name anywhere in `app.py`/`main.py`/`src/gowri_proj/*.py`/`tests/*.py`. Correctly
transitive; no action needed.

### Dev

- **pytest (9.1.1)** — Keep. Test runner, actively used (93 tests, 16 files).
- **ruff (0.16.3)** — Keep. Introduced by Agent 1, now the standard lint/format tool.

## Overlapping-functionality check
No overlap found: exactly one Excel-read path (pandas + openpyxl/xlrd engines), one Excel-write
path (pandas + openpyxl), one HTTP framework (flask), one DataFrame library (pandas). No
redundant packages doing the same job.

## Unmaintained / vulnerable check
All 5 direct dependencies are on current, actively maintained releases as of this audit
(flask 3.1.3, pandas 3.0.5, numpy 2.5.1, openpyxl 3.1.5, xlrd 2.0.2). No known CVEs identified
for the pinned versions. No dependency flagged as abandoned/unmaintained.

## Changes implemented
- `pyproject.toml`: added `"numpy>=2.5.1"` to `[project.dependencies]` (+1 line).
- `uv.lock`: regenerated via `uv sync`, numpy moved from implicit-transitive to
  explicit-direct entry (+2 lines, no version/count change — still 23 resolved packages).

No source code changes. No removals or replacements implemented — every existing direct
dependency (flask, openpyxl, pandas, xlrd) has concrete, verified, non-trivial usage and no
safe stdlib/lighter alternative that wouldn't be a large behavioral-risk rewrite.

## Recommendations NOT implemented (left for humans)
- None requiring code changes. The one actionable gap found (`.xls` parsing path has no direct
  test coverage) is a **test-coverage** finding, not a dependency finding — flagged for
  Agent 4 rather than acted on here, since adding new tests is outside this agent's remit
  (dependency audit) and risks scope creep into another agent's assigned tier.

## Files touched
- `pyproject.toml` (+1)
- `uv.lock` (+2, regenerated, no version drift)

## Validation (FULL tier, as assigned)
- `uv sync`: resolves cleanly, 23 packages (same count; numpy now explicit instead of implicit).
- `.venv/bin/pytest -q`: **93 passed**, 0 failed — same as Agent 2's handoff. Runtime ~1.3s.
- `ruff check .`: All checks passed.
- `ruff format --check .`: 30 files already formatted.

## Notes for Agent 4 (Test Quality Audit)
- **No test currently exercises real `.xls` (legacy Excel binary format) parsing.** All Excel
  test fixtures across `tests/test_stock_statement_duplicate_skus.py`,
  `tests/test_webapp_catalog_upload.py`, and `tests/test_item_catalog.py` build fixtures via
  `openpyxl.Workbook()`, which only produces `.xlsx`. `xlrd` (the `.xls` engine) is a real,
  load-bearing dependency for the primary documented input format
  (`SUPPORTED_SUFFIXES = {".xls", ".xlsx"}` in `sync.py`, README explicitly calls out `.xls`
  pharmacy-software exports as the main use case) but has zero direct test coverage. Worth a
  test using a genuine `.xls` fixture (e.g. via `xlwt` for generation, or a small committed
  binary fixture) to catch any future regression in that code path.
- No dependency changes beyond the numpy fix should affect your test-quality review — the
  numpy addition is metadata-only (no version/behavior change).
