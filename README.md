# Inventory Dashboard

Tracks pharmacy inventory (stock, sales, dead/low/overstock SKUs) from the
"Stock Statement" `.xls` exports your pharmacy software produces, across as
many months as you import. Runs entirely on your computer — a SQLite
database on disk, and a local web app in your browser. Nothing is uploaded
anywhere.

## Setup on a new device (one time)

1. Copy this whole project folder onto the device.
2. **Mac**: double-click `install.command`. **Windows**: double-click
   `install.bat`. This installs `uv` (the tool that runs the app — no
   separate Python install needed) and downloads the app's dependencies.
   A window opens showing progress; press Enter/close it when it says
   "Setup complete".

If double-clicking a `.command` file does nothing on Mac (Gatekeeper can
block it the first time), right-click it → Open instead. If a script fails
partway, running it again picks up where it left off.

## Getting updates

- **Mac**: double-click `update.command`
- **Windows**: double-click `update.bat`

Pulls the latest version of the app and installs any new dependencies —
your imported reports, uploads, and settings all live in files this never
touches (`db/`, `uploads/`, `output/`), so they're untouched no matter what.
Safe to run any time; it says "Already up to date" if there's nothing new.
Close the app first if it's running. Requires the folder to have been set
up to receive updates (ask whoever set this up on this device if
`update.command`/`update.bat` says it isn't).

## Daily use

- **Mac**: double-click `run.command`
- **Windows**: double-click `run.bat`

A terminal window opens (leave it running) and your browser opens to the
app automatically at `http://127.0.0.1:8765`. Close the terminal window to
stop the app. Everything from here on is point-and-click:

- **Reports page** — drag & drop (or click to browse) each month's
  `.xls`/`.xlsx` export to import it; a **Rescan uploads folder** button
  picks up anything dropped straight into the `uploads/` folder; each
  imported report has a **Remove** button (with a confirmation prompt) for
  fixing a wrong upload.
- **Dashboard page** — stock health (out-of-stock, low-stock, dead-stock,
  overstock), top brands/SKUs by value, month-over-month trend charts (once
  2+ months are imported), and searchable/sortable action lists with an
  **Export CSV** button on each. Click any brand or SKU — in the top-value
  charts, the trend legend, or an action-list row — to open its own detail
  panel: full period-by-period history and a mini trend chart; a brand's
  panel lists its SKUs, each clickable in turn. An empty action list says so
  plainly ("Nothing is out of stock") rather than looking broken. The Dead
  Stock tab also breaks its list down by **age** (90–179 / 180–364 / 365+
  days since anything last happened to that SKU) — a SKU dead 3 months and
  one dead 2 years call for very different actions.
- **Search box** (top of every page) — looks up any SKU across your entire
  current catalog, not just within one action-list tab. The dashboard's
  per-tab search boxes only cover out-of-stock/low-stock/dead-stock/
  overstock; this also finds "healthy" SKUs, which aren't listed anywhere
  else. Click a result to open its detail panel.
- **Settings page** — `low_stock_days`, `overstock_days`, `dead_stock_days`,
  and the sales-pace trailing window are editable here instead of being
  fixed constants. Takes effect immediately, everywhere (dashboard, and the
  CLI's defaults — see below) — no restart needed.
- **Import health** (on the Reports page) — last refresh time, any gap
  between when one imported report ends and the next begins, and a list of
  files a rescan rejected (wrong period, duplicate, unreadable). This
  catches *missing reporting periods*; it does not (yet) catch a SKU that
  quietly disappears from one report to the next while the reporting period
  itself stays fully covered — see "Current-state blind spot" below, which
  is a different, currently-undetected problem.

The very first file can span a wider range (e.g. an initial Apr–Jul export)
as a starting baseline — everything after that should be one month at a
time, in any order (you can backfill earlier months later; the dashboard
just needs the *latest* period for current stock and enough recent periods
for the sales-pace trend).

## Fixing a bad upload

- **Two files for the same month** — can't happen silently. Reports have a
  hard uniqueness constraint on the period, so uploading (or rescanning) a
  second file covering an already-imported period is rejected with a clear
  message instead of double-counting.
- **Wrong data in an already-imported file** — re-upload the corrected file
  with the same filename (via the Reports page, or by overwriting it in
  `uploads/` and rescanning). It replaces that report; nothing duplicates.
- **A report shouldn't have existed at all** (wrong store, wrong period) —
  click **Remove** next to it on the Reports page. If its source file is
  still in `uploads/`, the next rescan will treat it as new and re-import it
  — delete or fix that file too if you don't want it to come back (the app
  tells you this in the confirmation prompt).
- **A filename now covers a different period than it used to** (e.g. you
  saved a genuinely different month's export over an old one without
  meaning to) — the app won't silently create a second report while leaving
  the old one orphaned. It's rejected with a message telling you to remove
  the old report first if the new file is correct.

## How the numbers are calculated

All four thresholds below are editable from the **Settings** page (and via
matching CLI flags — see below); the numbers here are just the built-in
defaults.

- **Current stock** (closing stock, value) comes from the most recently
  imported report.
- **Sales pace** ("days of cover") is a rolling average: total sales summed
  across the most recent reports, going back at least `trailing_days_target`
  (default 90) days, divided by those days. It's recomputed fresh every time
  you view the dashboard, so it always reflects whichever reports currently
  fall in that trailing window — a strong month and a weak month within the
  window get blended together rather than the latest month dominating. As
  you import more months the window slides forward and the oldest month
  drops out.
- **Dead stock** is judged differently from the other buckets — it's not
  part of that shared trailing window. A SKU is dead stock once at least
  `dead_stock_days` (default 90) days have passed since the *most recent* of
  its last purchase or its last sale — whichever is more recent — with zero
  activity since. A SKU restocked last month that hasn't sold yet isn't
  dead; one restocked 4 months ago that still hasn't moved is; one that sold
  a handful of units right after being restocked a year ago but nothing
  since is dead too, once enough time has passed since *that* sale (a single
  old sale doesn't grant permanent immunity). If a SKU has neither sold nor
  been purchased anywhere in your imported history, the reference point
  falls back to the earliest report you've imported. The Dead Stock tab
  further splits this list into 90–179 / 180–364 / 365+ day-old buckets.
- **Other status buckets**: `out_of_stock` (zero on hand), `low_stock`
  (below `low_stock_days`, default 15, days of cover), `overstock` (above
  `overstock_days`, default 90, days of cover), `healthy` (everything else).
- **Data quality**: every imported row is checked for combinations that are
  logically impossible (positive stock with negative value, negative stock,
  negative sales) — not corrected, since there's no way to know the true
  number, just surfaced in a "Data quality" card on the dashboard (only
  appears when something's actually flagged) so a bad source export doesn't
  silently skew a total or show up as an unexplained dip in a trend chart.
- **Current-state blind spot**: only the most recently imported report
  determines "what's currently on the shelf." A SKU that had real stock in
  an earlier report but is simply absent from the latest one (rather than
  showing zero) won't appear anywhere in the dashboard — not out-of-stock,
  not in any total. This can happen at reporting boundaries (e.g. a
  financial-year rollover) where a source export just stops listing certain
  items. There's no automatic detection for this yet.

## Command line (optional, for power users / automation)

Everything the web app does is also available from the CLI:

```bash
uv run main.py refresh --open      # scan uploads/, import anything new, rebuild the dashboard
uv run main.py import <file.xls>   # load one specific file from anywhere
uv run main.py list                # see what's been imported (and any coverage gaps)
uv run main.py remove <id>         # delete a report
uv run main.py dashboard --excel   # rebuild the dashboard + write inventory_lists.xlsx
```

`dashboard`/`refresh` read the same thresholds saved on the Settings page by
default. Pass `--low-stock-days`, `--overstock-days`, `--trailing-days`, or
`--dead-stock-days` for a one-off override on that run only — it's not
written back to Settings.

## Project layout

```
app.py                 web app entry point — `uv run app.py` (or double-click run.command/run.bat)
install.command          Mac one-time setup (installs uv + dependencies)
install.bat              Windows one-time setup
run.command             Mac double-click launcher
run.bat                 Windows double-click launcher
main.py                 CLI entry point (refresh / import / list / remove / dashboard)
src/gowri_proj/
  webapp.py             Flask routes — dashboard/reports/settings pages, all /api/* endpoints
  templates/
    base.html           shared nav, theme toggle, toasts, confirm modal, global search,
                         and the SKU/brand detail panel + line-chart code (used from any page)
    dashboard.html      KPIs, stock health, dead-stock aging, charts, trend lines, action lists
    reports.html        upload dropzone, reports table, remove, import-health card
    settings.html       editable status thresholds
  parser.py             parses one .xls export into a tidy DataFrame
  db.py                 SQLite storage — reports / stock_entries / watched_files / settings
  sync.py               scans uploads/ and imports whatever is new
  analysis.py           combines all imported reports into the current summary + trends +
                         dead-stock aging + import-gap detection + global search
  dashboard.py           builds the dashboard's JSON payload (shared by web app + CLI)
  excel_export.py       multi-sheet Excel export
uploads/                drop each month's .xls/.xlsx export here (gitignored)
db/inventory.db         SQLite database (gitignored)
output/                 CLI-generated dashboard/Excel exports (gitignored)
```
