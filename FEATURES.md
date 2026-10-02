# Feature log

Every feature the app has, and how to pull one back out if it breaks.

## How features ship (and how to recall one)

The pharmacy's copy runs whatever is on GitHub's `main`: `update.bat`/
`update.command` do `git reset --hard origin/main`. So each feature lands on
`main` as exactly **one merge commit**, which makes it removable on its own:

1. Build it on its own branch, `feature/<name>`, branched from `main`.
2. Merge with `git merge --no-ff feature/<name>` (always a real merge commit,
   never a fast-forward) and tag that merge commit `merged/<name>`:
   `git tag -a merged/<name> -m "<one line>"`. (Not the branch's own name —
   a tag and a branch sharing a name makes git call it "ambiguous".)
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
git switch feature/<name>          # or recreate it: git switch -c feature/<name> merged/<name>^2
# ...commit the fix on the branch...
git switch main
git revert <hash of the "Revert ..." commit>
git merge --no-ff feature/<name>
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
| Value segments download: "Download all (Excel)" + per-tile CSV on the Dashboard | 2026-10-01 | `bc383a8` | `merged/value-segment-export` | No |
| History popups: month-by-month chart + table for each status row and the on-hand KPI tiles | 2026-10-01 | `e333cec` | `merged/status-history` | No |
| Fix: rescan uses the most recently saved of two overlapping files, and stops re-importing both every time | 2026-10-01 | `7f73c05` | `merged/partial-period-supersede` | No schema change (see below) |
| Browser-tab icon (no more /favicon.ico 404) | 2026-10-01 | `cb3e0c2` | `merged/favicon` | No |
| Every page fits phone width (top bar wraps; status rows stack; Reports tables scroll in place) | 2026-10-01 | `485fd79` | `merged/mobile-nav` | No |
| Data-quality warning on Reports can be dismissed | 2026-10-01 | `e47e0ce` | `merged/dismiss-data-quality` | No |
| Renamed items: join up history across name changes; review uncertain renames | 2026-10-01 | `9cab877` | `merged/sku-identity` | New table (see below) |
| Overstock only once a product has been around for `overstock_days` | 2026-10-01 | `a521356` | `merged/overstock-min-age` | No |
| Dead stock: any real stock arrival resets the clock, not only paid purchases | 2026-10-01 | `ebd193b` | `merged/dead-stock-arrivals` | No |
| Renamed items: changed pack size/strength never suggested; long-name/prefix codes need same spelling | 2026-10-01 | `bebab11` | `merged/review-skip-size-changes` | No |
| Review page + top-bar tab with count badge | 2026-10-01 | `07cb4b7` | `merged/review-page` | No |
| Review keyboard shortcuts (depends on review page) | 2026-10-01 | `c5ee3db` | `merged/review-shortcuts` | No |
| KPI tiles: change since last month, short amounts | 2026-10-01 | `ce1cc1b` | `merged/kpi-tiles` | No |
| Dashboard: Download all lists (Excel) | 2026-10-01 | `9e5d5f7` | `merged/excel-all-lists` | No |
| Dashboard: stale-data notice (>35 days) | 2026-10-01 | `f796fd3` | `merged/stale-data-banner` | No |
| Value segments: share of stock value per tile | 2026-10-01 | `b4435e6` | `merged/segment-value-share` | No |
| Reminder to upload a fresh item list when older than the latest report | 2026-10-01 | `a857b79` | `merged/item-list-reminder` | New table (see below) |
| Rescan recognises the item list instead of rejecting it | 2026-10-01 | `3d24439` | `merged/rescan-item-list` | No |
| Dated item list copies (last 6 months); rescan imports a newer list (depends on rescan fix) | 2026-10-01 | `f4a731d` | `merged/item-list-archive` | No |
| Split webapp.py into focused modules (no behaviour change; everything below builds on it) | 2026-10-01 | `6d2efe6` | `merged/split-webapp` | No |
| Fix: a bad or locked item list no longer aborts a rescan | 2026-10-01 | `4759ead` | `merged/rescan-item-list-robust` | No |
| Fix: "not in your item list" allows for the 25-character cut-off (721 → 82) | 2026-10-01 | `215e9f1` | `merged/unmatched-truncated-names` | No |
| Fix: CLI import reports bad files in plain English | 2026-10-01 | `b34a85d` | `merged/cli-import-errors` | No |
| Fix: low stock must be fewer days than overstock | 2026-10-01 | `6f3c756` | `merged/settings-threshold-order` | No |
| Fix: data quality shows the source file's name | 2026-10-01 | `9698746` | `merged/quality-issue-source-names` | No |
| Fix: non-Latin filenames; wrong-upload-box message | 2026-10-01 | `839c239` | `merged/upload-names-and-wrong-box` | No |
| Fix: stable order for equal values (no reshuffle between runs) | 2026-10-01 | `1817cf3` | `merged/stable-tie-order` | No |
| Fix: calendar dates shown from their text (no day-early dates) | 2026-10-01 | `23fbfe9` | `merged/date-display` | No |
| Faster imports: parsers work on whole columns (~5x faster rescans) | 2026-10-01 | `1f225a1` | `merged/fast-parsers` | No |
| Tests no longer write into the app's real logs/error.log | 2026-10-01 | `55acb28` | `merged/tests-isolated-logs` | No |

### Code audit, 2026-10-01 — restructure, 8 fixes, faster imports

Found by reading every module, hitting every page/endpoint in three
database states (empty, one report, real data) with good and bad input,
profiling, and checking statuses/segments SKU by SKU against an independent
reference implementation. Each fix has a regression test that fails on the
old code.

**Revert order.** Everything below builds on the restructure, so it can only
be reverted after all of them. Two pairs must also be reverted in order:
`1817cf3` (stable order) before `215e9f1` (item-list check — they share a
test file), and `1f225a1` (fast parsers) before `839c239` (upload fixes —
neighbouring code in parser.py). All others revert on their own.

- **Restructure** `6d2efe6` — `git revert -m 1 6d2efe6` (last). `webapp.py`
  (~1,400 lines, every route nested in `create_app`) split into
  `datacache.py`, `web_helpers.py` and `routes/` (pages, settings,
  inventory, review, imports, exports). Route bodies moved verbatim, endpoint
  names unchanged; every page/endpoint's output on real data byte-identical.
- **Rescan robustness** `4759ead` — an item list that failed to import
  (e.g. duplicate code) or whose copy hit a file lock aborted the whole
  rescan; now that file is reported and the rest import.
- **Item-list check** `215e9f1` — "SKUs not in your item list" compared
  whole names, but the item list cuts names to 25 characters: 721 reported,
  82 real. Rule shared with rename detection (`identity.catalog_name_keys`).
- **CLI import** `b34a85d` — a bad file printed a traceback; now the plain
  message.
- **Settings order** `6f3c756` — low stock ≥ overstock was accepted (nothing
  could be healthy); now refused, in Settings and the CLI.
- **Data-quality names** `9698746` — issues showed an item's merged (newer)
  name, not the name in the file. Latent on current data.
- **Uploads** `839c239` — a non-Latin filename was rejected as "not
  .xls/.xlsx"; a file in the wrong upload box gave a cryptic error.
- **Stable order** `1817cf3` — equal values reordered between app starts
  (set order), changing which items made capped lists.
- **Dates** `23fbfe9` — report periods showed a day early on computers
  behind UTC ("31 Mar" for "1 Apr"); calendar dates now formatted from their
  text by one shared `fmtIsoDate()` (replacing four copies). India-time
  output unchanged.
- **Fast parsers** `1f225a1` — row-by-row parsing was ~1.8s per statement;
  column operations are ~11x faster with identical output on all 17 real
  files. Rescan reads each file once. Fresh 17-file rescan 35.4s → 6.5s.
  An item-list row without a code is now skipped instead of keyed "nan".

### Rescan recognises the item list — `fix/rescan-item-list`

Recall: revert `feature/item-list-archive` **first** (it builds on this),
then `git revert -m 1 3d24439`.

- Rescan used to parse `uploads/item_catalog.xlsx` as a stock statement and
  list it under "Files rejected". It now recognises an item list by its
  "Item List as on" banner (any filename) and records it as status
  `item_list`, which isn't a rejection. An unchanged file previously
  rejected is re-checked, so the old false entry clears itself.
- Tests: `tests/test_sync_item_list.py`.

### Item list archive — `feature/item-list-archive`

Recall: `git revert -m 1 f4a731d`

- Each item list that becomes current is also kept as
  `uploads/item_lists/<FY>/item_list_<date>.<ext>`; only the last 6 months
  (183 days back from the newest list's date) are kept. The cleanup only
  deletes files matching that name pattern under `item_lists/`.
- Rescan imports an item list found anywhere in `uploads/` if it's newer
  than the current one (copies it to `item_catalog.xlsx`, archives it);
  older or undated lists are noted, never imported.
- Tests: `tests/test_item_list_archive.py`.

### Item list reminder — `feature/item-list-reminder`

Recall: `git revert -m 1 a857b79`

- When the item list is older than the latest report: "Your item list is
  from 9 Aug and the latest report is 17 Aug. Upload a fresh one so renames
  are recognised automatically." — toast after a stock-statement upload,
  and an Import health row (counted as an issue) with an "Upload item list"
  button. Says so if no list was ever uploaded.
- DB: adds one-row table `item_catalog_info` holding the list's own "as on"
  date (written only after a successful upload; NULL if the file had none).
  Lists uploaded earlier fall back to their upload day. Reverting leaves the
  table unused — harmless.
- Tests: `tests/test_item_list_reminder.py`.

### Review list rules tightened — `fix/review-skip-size-changes`

Recall: `git revert -m 1 bebab11`

- A pair where a number was *replaced* (strength, pack size, count —
  "CLINDAC A GEL 20GM" → "30GM") is never suggested; one where a number was
  only added/removed still is. A code matched only via the item list's long
  name or 25-character cut-off merges automatically only with the same
  spelling (the item list has codes whose short and long names name
  different variants). Spelling match also tolerates a moved space next to
  a unit. The renamed-items export fills in brands for merged rows.
- Tests: `tests/test_identity.py`, `tests/test_sku_merges_api.py`.

### Review page — `feature/review-page`

Recall: revert `feature/review-shortcuts` **first** (it builds on this), then
`git revert -m 1 07cb4b7`.

- `/review` holds the renamed-items card (moved from Reports); a "Review"
  tab in the top bar shows the pending count (`GET /api/review-count`,
  fetched after page load). Import health links to it.

### Review keyboard shortcuts — `feature/review-shortcuts`

Recall: `git revert -m 1 c5ee3db`

- ↑/↓ or J/K move a highlighted row; M merge, N not the same, U undo;
  after a decision the cursor moves to the next undecided row. Ignored
  while typing or with a dialog open.

### KPI tiles — `feature/kpi-tiles`

Recall: `git revert -m 1 ce1cc1b`

- Each tile shows the change vs the last report ending in an earlier
  calendar month (`kpis_previous` in the payload, from new per-report
  totals in the trend series); the value tile reads "₹1.43 Cr" with the
  exact amount in its tooltip (`fmtINRShort` in base.html).
- Tests: `tests/test_kpi_tiles.py`.

### Download all lists — `feature/excel-all-lists`

Recall: `git revert -m 1 9e5d5f7`

- `GET /api/action-lists/export.xlsx`: Summary + one sheet per action list,
  every row; same workbook as the CLI's `--excel` (shared code).
- Tests: `tests/test_action_lists_export.py`.

### Stale-data notice — `feature/stale-data-banner`

Recall: `git revert -m 1 f796fd3`

- Dashboard notice when the latest import is more than `STALE_DATA_DAYS`
  (35) days old, by this computer's local date.
- Tests: `tests/test_stale_data_banner.py`.

### Segment value share — `feature/segment-value-share`

Recall: `git revert -m 1 b4435e6`

- Each value-segment tile adds "N% of stock value" (share of all nine).

### Renamed items — `feature/sku-identity`

Recall: `git revert -m 1 9cab877`

- New `identity.py` groups stock-statement names into items at load time;
  every analysis then sees one item under its latest name. Stored rows are
  never changed. Automatic only on: same item code (a code matched only on
  the item list's 25-character name cut-off also needs identical numbers),
  pharmacy tag differences ((NON), ZZ, (RECALL)), or identical spelling with
  stock carried over exactly. Different numbers/words are never automatic.
- Reports → **Renamed items** card: To review (approve/reject, 50 at a
  time), Merged automatically (split any), Your decisions (undo any).
  Search finds items by old names; SKU panel shows "Also listed as".
- "Returned" now judges "nothing sold" since the item got its current name,
  so a (NON)-tagged item sent back isn't shown as out of stock.
- DB: adds table `sku_merge_decisions` (CREATE IF NOT EXISTS). Reverting
  the code leaves it in place, unused — harmless; decisions come back if the
  feature is re-applied.
- Endpoints: `GET /api/sku-merges`, `POST /api/sku-merges`,
  `POST /api/sku-merges/undo`, `GET /api/sku-merges/export.csv`.
- Tests: `tests/test_identity.py`, `tests/test_sku_merges_api.py`.

### Overstock minimum age — `fix/overstock-min-age`

Recall: `git revert -m 1 a521356`

- Overstock requires the product's age (from the end of the first report it
  appears in) ≥ `overstock_days`; younger ones show Healthy. Products in the
  earliest report, or first appearing with opening stock, count as old.
  Value segments still call such a product Slow (pace, not age).
- Tests: `tests/test_overstock_min_age.py`.

### Dead stock counts real arrivals — `fix/dead-stock-arrivals`

Recall: `git revert -m 1 ebd193b`

- The dead-stock clock resets on a paid purchase, free/scheme units, or a
  transfer in larger than same-period returns/adjustments. Swaps (5 in, 5
  out) don't reset it.
- Tests: `tests/test_dead_stock_arrivals.py`.

### Verification (2026-10-01)

Action lists and Value segments were checked SKU by SKU against an
independent reference implementation of the documented rules, on real data
with saved settings and on the uploads data with defaults — every status,
action-list figure, status-bar total, value tier, movement and tile matched,
before and after these three changes.

### Dismissible data-quality warning — `feature/dismiss-data-quality`

Recall: `git revert -m 1 e47e0ce`

- Reports → Import health → data-quality warning gets **Dismiss**. It
  collapses to "N data quality issues dismissed · Show" and stops counting
  toward the header's issue badge; if it was the only issue, the badge turns
  green and reads "show dismissed" (click to bring it back).
- Remembered per browser (`localStorage` key `inv-quality-dismissed`), keyed
  to a fingerprint of the exact issue set from `/api/import-health`
  (`quality_issues.fingerprint`). A later import with a different set of
  issues brings the warning back automatically.
- Touches: `webapp.py` (`quality_issues_fingerprint`), `templates/reports.html`;
  tests in `tests/test_quality_dismiss.py`.

### Phone-width layout — `fix/mobile-nav`

Recall: `git revert -m 1 485fd79`

- Below 760px: the top bar wraps (brand + icon-only theme toggle, then nav,
  then full-width search) and isn't sticky; Dashboard status rows stack;
  Trends leaderboard hover tooltips are hidden; Reports tables scroll
  sideways inside a `.table-x` box. Desktop layout unchanged.
- Checked at 360/390/768/1280px on all four pages: no sideways page scroll.
- Touches: `templates/base.html`, `dashboard.html`, `trends.html`, `reports.html` (CSS,
  plus the `.table-x` wrapper around Reports tables).

### Browser-tab icon — `fix/favicon`

Recall: `git revert -m 1 cb3e0c2`

- Inline SVG (stacked boxes, accent teal) served at `/favicon.svg` and
  `/favicon.ico`, linked from `base.html`. Touches: `webapp.py`, `base.html`;
  tests in `tests/test_favicon.py`.

### Overlapping files on rescan — `fix/partial-period-supersede`

Recall: `git revert -m 1 7f73c05`

- "Newest upload wins" for overlapping periods now means most recently
  **saved** file (mtime), not alphabetically last. The older file is
  recorded in `watched_files` as status `superseded` (shown under Import
  health with the reason) and skipped on later scans; removing the winning
  report lets it back in. An already-wrong database repairs itself on the
  next rescan.
- DB: no schema change. Writes `superseded` rows to `watched_files`; code
  from before this fix just treats those as unchanged files, so a revert is
  safe.
- Touches: `db.py` (`find_newer_overlapping_source`), `sync.py`, `main.py`,
  `webapp.py`, `templates/reports.html` (rescan toast); tests in
  `tests/test_sync_overlapping_files.py`.

### History popups — `feature/status-history`

Recall: `git revert -m 1 e333cec`

- Dashboard → each status row (Out of stock … Returned) has a **History**
  button; the **Total SKUs**, **Inventory value on hand** and **Units on
  hand** tiles are clickable. Each opens a popup: chart(s) plus a by-month
  table with month-over-month change.
- Each month's point is `summarize_history()` re-run over only the reports
  imported by then (as if that month were the latest import), judged with
  *today's* Settings. The latest point is identical to the dashboard by
  construction (tested). Early months with under `trailing_days_target` days
  of sales or under `dead_stock_days` of history are marked "≈ approximate".
- One point per calendar month (that month's latest report). Computed on
  first request, then cached until data or Settings change (~3s cold for
  17 months on a Mac; the page pre-warms it in the background).
- Endpoint: `GET /api/status-history`.
- Touches: `analysis.py` (new `status_history`, existing code unchanged),
  `webapp.py`, `templates/dashboard.html`; tests in `tests/test_status_history.py`.

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
