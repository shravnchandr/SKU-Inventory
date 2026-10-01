# Feature log

Every feature the app has, and how to pull one back out if it breaks.

## How features ship (and how to recall one)

The pharmacy's copy runs whatever is on GitHub's `main`: `update.bat`/
`update.command` do `git reset --hard origin/main`. So each feature lands on
`main` as exactly **one merge commit**, which makes it removable on its own:

1. Build it on its own branch, `feature/<name>`, branched from `main`.
2. Merge with `git merge --no-ff feature/<name>` (always a real merge commit,
   never a fast-forward) and tag that merge commit with the same name:
   `git tag -a feature/<name> -m "<one line>"`.
3. Add a row to the table below with the merge hash.

**To recall a broken feature** while everything else keeps working:

```bash
git revert -m 1 <merge hash>     # adds a new commit undoing just that feature
git push                         # then run update.bat on the pharmacy PC
```

**To bring it back once fixed:** a reverted merge can't simply be merged
again, because git considers those commits already in `main`. Revert the revert
first, then merge the fix on top:

```bash
git revert <hash of the "Revert ..." commit>
git merge --no-ff feature/<name>   # with the fix committed on that branch
```

Rules that keep this safe:

- **Never rewrite `main`** (no force-push, no rebasing it). A revert has to
  reach the pharmacy as an ordinary new commit.
- **Note any database change.** The SQLite database (`db/`) isn't in git,
  so reverting code doesn't undo a schema change it made. A feature marked
  "DB change: yes" needs its own rollback plan before it ships.
- **Before merging,** the full test suite passes, and reverting the merge on
  a throwaway branch also leaves the test suite passing.

## Features

| Feature | Merged | Merge commit | Tag | DB change |
|---|---|---|---|---|
| Everything up to and including the file-lock/update-banner work (see below) | before this log | `fdb32bd` | `baseline-2026-10-01` | — |
| Value segments download: "Download all (Excel)" + per-tile CSV on the Dashboard | 2026-10-01 | `bc383a8` | `feature/value-segment-export` | No |

### Value segments download — `feature/value-segment-export`

Recall: `git revert -m 1 bc383a8`

- Dashboard → Value segments card: **Download all (Excel)** gives a workbook
  with a Summary sheet (9 tiles: SKUs, value, recommendation) plus one sheet
  per segment. The selected tile's list has its own **Export CSV**.
- Download columns: Brand, SKU, Closing stock, Value, Sold (trailing window),
  Days of cover, Days since activity.
- Endpoints: `GET /api/value-segments/export.xlsx`,
  `GET /api/value-segments/export.csv?tier=A|B|C&movement=fast|slow|non_moving`.
- Touches: `excel_export.py`, `webapp.py`, `templates/dashboard.html`;
  tests in `tests/test_value_segment_export.py`.

### Baseline (before this log)

These were built before the branch-per-feature workflow, so they can't be
recalled one at a time. `git checkout baseline-2026-10-01` gets back to this
exact state if ever needed.

- Import of stock statement `.xls`/`.xlsx` exports (upload, rescan of
  `uploads/`, CLI), with duplicate-period, overlap and reused-filename protection
- Dashboard: KPIs, stock-health breakdown, action lists (out of stock / low /
  dead / overstock) with search, sort and CSV export
- Dead-stock aging buckets
- Value segments (ABC tier × movement), 9 tiles with SKU lists
- Trends page: leaderboards and month-over-month charts
- SKU and brand detail panels; global SKU search
- Reports page: import health (coverage gaps, rejected files, SKU churn),
  item code list upload for rename detection
- Settings page: editable thresholds
- Data-quality checks
- Update-available banner; readable error messages with `logs/error.log`
- Windows/Mac install, run and update scripts
