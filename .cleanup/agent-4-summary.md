# Agent 4 — Test Quality Audit — Summary

## Method
Read `.cleanup/00-baseline.md` and `agent-1-summary.md` through `agent-3-summary.md`
first. Agent 3's summary explicitly flagged that no test exercises real `.xls`/xlrd
parsing (every fixture in the suite is built via `openpyxl.Workbook()`, which only
produces `.xlsx`) — treated this as the primary "missing coverage" lead per the task
brief. Snapshotted the suite via `pytest --collect-only -q`, then read every one of
the 15 test files in full (2,269 lines total) and evaluated each test case against
the removal/skip/missing criteria in the assignment.

## Test suite snapshot (reference point for later agents)
**Baseline: 93 tests collected across 16 files (15 `test_*.py` + empty `__init__.py`), 0 skipped.**

```
tests/test_brand_rename_regression.py       5 tests
tests/test_data_quality.py                  7 tests
tests/test_dead_stock.py                    7 tests
tests/test_import_gaps.py                   6 tests
tests/test_item_catalog.py                 20 tests
tests/test_report_overlap.py                7 tests
tests/test_report_period_tiebreak.py        2 tests
tests/test_sales_free_demand.py             4 tests
tests/test_status.py                       10 tests
tests/test_status_vectorization_consistency.py  1 test
tests/test_stock_entries_uniqueness.py      2 tests
tests/test_stock_statement_duplicate_skus.py 3 tests
tests/test_trailing_window_gaps.py          3 tests
tests/test_value_segments.py               14 tests (incl. 6 parametrize cases)
tests/test_webapp_catalog_upload.py         2 tests
```
Total = 93 (matches baseline doc and Agent 1/2/3's validation runs). No coverage-%
tool installed (per baseline) — this review is qualitative, based on reading every
test file's assertions and fixture construction, not a coverage-tool report.

Full `pytest --collect-only -q` output (93 test IDs) is reproducible from the repo
as-is; not duplicated here for brevity, but was captured and diffed against the
final run (95 tests, +2 new) to confirm no test IDs were lost.

## Findings: existing suite quality

**Every one of the 93 pre-existing tests was read and found to be genuinely
high-value: no trivial assertions, no dead-code tests, no byte-for-byte
duplicates, and — with exactly one justified exception — no mocking.**

- **Zero tests with `assert True` / no assertion / trivially-constant assertions.**
  Verified via targeted grep in addition to the full read-through.
- **Zero tests for removed code.** No test file references a function/module that
  doesn't exist in `src/gowri_proj/` (grep-verified against current source).
- **Zero byte-for-byte duplicate test cases.** Some fixture-builder helpers
  (`_row`, `_entries`) are near-identical across files (e.g.
  `test_brand_rename_regression.py`, `test_report_period_tiebreak.py`,
  `test_sales_free_demand.py`, `test_value_segments.py`, `test_trailing_window_gaps.py`
  all define their own local `_row(report_id, period_start, period_end, sku, ...)`
  helper with slightly different keyword defaults/columns per file's needs) — this
  is fixture duplication, not test-case duplication, and is explicitly out of this
  agent's remit per Agent 2's summary ("Did not touch tests/ ... a closer per-test
  dedup pass was out of scope"). Left alone; flagging for awareness only, not acting
  on it (touching shared test fixtures across 5 files carries real risk for a
  refactor-quality payoff, not a correctness one).
- **Mocking: exactly one `unittest.mock.patch` call in the entire suite**
  (`tests/test_webapp_catalog_upload.py`, mocking `os.replace` to simulate a
  disk-full/permissions failure after a DB commit but before the file write). This
  is well-justified in the file's own docstring — it's testing an error-rollback
  path that's real but "practically never happens" to trigger from a real OS error
  during a test run, and the mock is narrowly scoped to one syscall, not a broad
  swap of the system under test. **No action needed** — does not meet the
  "heavily mocked, testing the mock" pattern this task asks to flag.
- **No weak-assertion tests found.** Every test asserts on real domain output
  (specific SKU lists, specific numeric values via `==`/`pytest.approx`, specific
  status strings, specific error messages via `pytest.raises(..., match=...)`), not
  just "didn't crash" or type checks.

**Conclusion: nothing was removed, and nothing was marked `pytest.mark.skip`.**
The narrow deletion bar in the task brief (no-assertion tests / dead-code tests /
literal duplicates) and the skip-worthy bar (heavily-mocked / weak-assertion /
otherwise low-value) both came up empty across the full suite. This is a
genuinely well-written test suite — every file carries a docstring explaining
*why* the test exists (often tied to a specific historical incident/regression),
and assertions are concrete and behavior-specific throughout.

## Findings: missing coverage

1. **`.xls`/xlrd parsing path — zero direct coverage before this pass (flagged by
   Agent 3, now fixed).** `.xls` is the primary documented input format (README,
   `sync.py`'s `SUPPORTED_SUFFIXES = {".xls", ".xlsx"}`, `webapp.py`'s upload
   validator) but goes through a completely different pandas engine (`xlrd`) than
   every existing test fixture (`openpyxl` -> `.xlsx` only). **Implemented**: see
   Changes below.

2. **Flask routes in `webapp.py` — thin coverage, NOT addressed this pass.**
   18 registered endpoints (`/`, `/dashboard`, `/trends`, `/api/search`,
   `/api/sku-detail`, `/api/brand-detail`, `/reports`, `/api/reports`,
   `/api/import-health`, `/settings`, `/api/settings`,
   `/api/reports/<id>/remove`, `/api/upload`, `/api/upload-item-list`,
   `/api/refresh`, plus 403/413 error handlers). Only `/api/upload-item-list` has
   direct test coverage (`test_webapp_catalog_upload.py`, 2 tests, both
   failure-path focused). `/api/upload` (the primary stock-statement upload route)
   and the 13 other routes have **no test coverage at all** — no smoke test that a
   GET returns 200, no test of `/api/upload`'s success path, no CSRF-token
   enforcement test (`X-CSRF-Token` header is required per the existing test's
   fixture setup, but nothing tests its *rejection* path), no test of the
   403/413 error handlers. **Not implemented this pass** — see Recommendations
   below for why.

3. **Untested error/edge-case paths beyond the above:** not systematically
   inventoried beyond what surfaced while reading each file (each file already
   covers its own module's key edge cases well, per the findings above). No
   additional high-risk gap stood out strongly enough to justify a new test
   within this pass's scope, once the `.xls` path (the one concretely flagged by
   the prior agent) was addressed.

## Changes implemented

**Added `tests/test_xls_legacy_format.py`** (2 new tests, +129 lines):
- `test_parses_a_real_xls_file_via_the_xlrd_engine` — builds a genuine binary
  `.xls` file (via `xlwt`, a new **dev-only** dependency — never imported by
  application code, only by this test to author the fixture) mirroring the exact
  layout `tests/test_stock_statement_duplicate_skus.py` already uses for `.xlsx`,
  and asserts `parse_stock_statement` reads it correctly end-to-end: SKU list,
  company/location/period metadata, and every numeric column.
- `test_duplicate_skus_within_a_real_xls_file_are_still_summed` — re-verifies the
  duplicate-SKU-summing regression (already covered for `.xlsx`) against the real
  `.xls`/xlrd path specifically, since xlrd's numeric dtype handling is a
  plausible place for silent divergence from openpyxl's.

Both tests exercise `pandas.read_excel(..., engine=xlrd)` for real — no mocking,
no monkeypatching of the engine dispatch. Verified they fail if the `.xls` file is
malformed (spot-checked by temporarily corrupting a header cell) before finalizing,
confirming they're not vacuously passing.

**No source code was changed.** No behavior was touched — this is pure test
addition plus a new dev dependency declaration.

## Files touched
- `tests/test_xls_legacy_format.py` — new file, +129 lines (2 tests).
- `pyproject.toml` — +1 line (`xlwt>=1.3.0` added to `[dependency-groups] dev`).
- `uv.lock` — regenerated, +11 lines (xlwt + itself, dev-only; runtime dependency
  set unchanged — verified `uv sync` still reports the same 5 direct runtime deps,
  now 6 dev/direct entries total incl. xlwt).

## Suspected pre-existing bugs surfaced — NONE
The two new `.xls` tests **passed on first correct implementation** — no bug was
uncovered in the `.xls`/xlrd parsing path. (One earlier iteration of the fixture
builder had a test-authoring mistake on my part — an off-by-one on row indices —
which was a bug in the *new test*, not the source; fixed before finalizing, not
a source change.) No other new test was written that could have surfaced a
pre-existing bug, so there is nothing to flag for human review from this pass.

## Recommendations NOT implemented (left for humans / later agents)

- **Flask route coverage (finding #2 above) — not implemented.** This is the
  single largest remaining coverage gap in the codebase: 13 of 15 non-upload
  routes have zero tests. Deliberately left out of this pass because:
  - It's a materially larger scope than "the highest-risk untested path" the
    task asks to prioritize — properly covering 13 routes (success + auth/CSRF
    + error paths for each) would be a multi-hour addition on its own, and risks
    turning a test-quality *audit* pass into a full route-test-suite build-out.
  - The `.xls` gap was the one concretely and specifically flagged by the prior
    agent as the highest-priority missing coverage; addressing that first and
    validating it thoroughly was judged the better use of this pass's scope than
    spreading thin across many new route tests.
  - A human/later agent should scope this as its own pass: at minimum, a smoke
    test per GET route (200 + expected top-level JSON/HTML shape) and a success-
    path test for `POST /api/upload` (the primary, currently-untested upload
    route — only its sibling `/api/upload-item-list` has coverage) would close
    most of the risk cheaply.
- **Shared test-fixture duplication across files** (the `_row`/`_entries` style
  helpers repeated with small variations in ~5 files) — noted above, not acted
  on; would be a DRY/simplification pass, not a test-quality-audit one, and
  Agent 2 (Deduplication) already explicitly scoped tests/ out of their pass.
- Did not add a coverage-percentage tool (e.g. `pytest-cov`) — out of scope per
  the task framing ("coverage is qualitative, not numeric" for this pass); a
  human can decide whether to adopt one project-wide later.

## Validation (FULL tier, as assigned)
- `uv sync`: resolves cleanly, 24 packages total (added `xlwt` as a dev-only dep;
  runtime dependency set — flask/numpy/openpyxl/pandas/xlrd — unchanged).
- `.venv/bin/pytest -q`: **95 passed**, 0 failed (93 pre-existing + 2 new). Runtime ~1.3s.
- `ruff check .`: All checks passed.
- `ruff format --check .`: 32 files already formatted (new test file included).

## Notes for Agent 5 (Circular Dependency Resolution)
- No import-graph changes made in this pass. The one new test file
  (`tests/test_xls_legacy_format.py`) imports only `xlwt` (new, test-only,
  stdlib-adjacent, no back-reference into `src/gowri_proj`) and
  `src.gowri_proj.parser.parse_stock_statement` — same import shape as every
  other parser test in the suite, nothing novel for a circular-dependency pass
  to worry about.
- `pyproject.toml`'s `[dependency-groups] dev` list gained one entry (`xlwt`);
  `[project.dependencies]` (runtime) is untouched from Agent 3's handoff state.
- If Agent 5's work touches `webapp.py`'s route registration or module-level
  imports, be aware the route-coverage gap documented above (finding #2) means
  a webapp.py import-cycle fix would currently have **no test safety net**
  beyond the 2 upload-route tests — worth being extra-careful with manual
  verification there specifically, or flagging the gap forward again if it's
  still unaddressed by the time later agents reach that module.
