# Pre-Flight Baseline — Cleanup Pipeline

Recorded: 2026-08-16
Baseline commit (pipeline starts on top of this): `3e1e990` (main, working tree clean)

## Working tree
Clean at pipeline start. No pre-existing uncommitted changes. Verified via `git status --short` (empty output).

## Repo shape
- 43 git-tracked files. Python project (Flask app: `gowri-proj`), managed with `uv`.
- Source: `src/gowri_proj/` — 8 modules (`sync.py`, `db.py`, `analysis.py`, `webapp.py`, `parser.py`,
  `dashboard.py`, `excel_export.py`, `__init__.py`), plus top-level `app.py`, `main.py`.
- Tests: `tests/` — 16 test files, pytest.
- No JS/TS, no other languages.

## Tooling present at baseline
- Build/deps: `uv` + `uv.lock` (22 resolved packages incl. transitive).
- Test runner: `pytest` (dev dependency), no `pytest-cov` — **coverage % is not measurable at baseline**
  without adding a new dependency, so coverage is reported qualitatively (test count / pass rate) instead.
- **No linter or formatter configured** — no ruff/black/flake8/isort config found anywhere in the repo
  (checked `pyproject.toml` and top-level config files). Per user decision, Agent 1 will introduce **ruff**
  (lint + format) as the standardized tool, since none currently exists to conform to.
- No typechecker configured (no mypy/pyright config) — Python is dynamically typed here; Agent 6's "type
  strengthening" will apply to type hints/annotations rather than a strict typechecker gate. No pre-existing
  typecheck baseline to compare against.
- No CI config found (no `.github/workflows`), so "the repository's configured checks" reduce to: `uv sync`
  (build/install) + `pytest` (tests). This is what FULL validation means for this repo. LIGHT validation =
  `uv sync` + `pytest` (no separate typecheck exists yet; lint/format checks apply once Agent 1 introduces ruff).
- `.venv` had `pyflakes` and `vulture` installed but **not declared** in `pyproject.toml`/`uv.lock` (`uv sync
  --dry-run` reports it would uninstall them) — stray local packages from prior ad hoc exploration, not part
  of the tracked dependency set. Not a pipeline concern; noted for completeness.

## Baseline validation run (FULL tier, pre-Agent-1)
- `uv sync --dry-run`: resolves cleanly, lockfile up to date.
- `pytest -q`: **93 passed**, 0 failed, 0 skipped. Runtime ~1.3s.
- No lint/format/typecheck/static-analysis to run yet (none configured) — this is itself a finding, addressed
  by Agent 1.
- No migration tooling detected; `db/inventory.db` exists but is gitignored (generated runtime data, not a
  tracked schema-migration system). Agent 13 will assess `src/gowri_proj/db.py` for schema/model hygiene
  against this file's *structure*, not its runtime data.

**Baseline is fully green — no pre-existing failures to carve out.** Every agent is responsible for the
entirety of its assigned validation tier.

## Baseline metrics (observational only, per METRICS note)
- Total tracked LOC: **9,367** (all tracked files); Python-only LOC: **5,171** across 26 `.py` files.
- Direct dependencies: **4** runtime (`flask`, `openpyxl`, `pandas`, `xlrd`) + **1** dev (`pytest`) = 5 declared.
- Resolved dependency graph (incl. transitive): **22** packages per `uv.lock`.
- Test count: **93** test cases across 16 files. No coverage % tool installed — not measurable numerically
  at baseline (see above).

## Decisions made with user before starting
- Agent 1 will introduce **ruff** (lint + format) as the project's standardized tool, since none existed.
- Running the **full 15-agent pipeline as specified**, not a scaled-down merge, despite the codebase being
  small (43 files) — user confirmed this explicitly.

## Execution note
Each agent below is run as an isolated subagent (fresh context per the Agent tool's normal semantics),
given only: this baseline file, the previous agent's handoff summary, and access to the git working tree/log —
consistent with the EXECUTION CONTEXT ISOLATION requirement.
