# Inventory Dashboard

Tracks pharmacy inventory — stock, sales, and which items are out of stock,
running low, not moving, or overstocked — from the monthly "Stock Statement"
exports your pharmacy software (POS) produces, across as many months as you
import. Runs entirely on your computer: a database file on disk and a local
web app in your browser. Nothing is uploaded anywhere.

## Setup on a new device (one time)

1. Copy this whole project folder onto the device.
2. **Windows**: double-click `install.bat`. **Mac**: double-click
   `install.command`. This installs `uv` (the tool that runs the app — no
   separate Python install needed) and the app's dependencies. A window
   shows progress; close it when it says "Setup complete".

If double-clicking a `.command` file does nothing on Mac (Gatekeeper can
block it the first time), right-click it → Open. If a script fails partway,
running it again picks up where it left off.

## Getting updates

- **Windows**: double-click `update.bat`
- **Mac**: double-click `update.command`

Close the app first. This fetches the latest version and installs any new
dependencies. Your data — imported reports, uploaded files, settings,
review decisions — lives in `db/` and `uploads/`, which updates never
touch. Safe to run any time; it says "Already up to date" if there's
nothing new. The app also shows a banner when an update is available.

If `git` (the tool that fetches updates) isn't installed, the script
installs it — via `winget` on Windows; via Homebrew or Apple's Command Line
Tools on Mac (a window pops up: click Install, wait, then run
`update.command` again). If neither works, it says where to download git.

## Daily use

- **Windows**: double-click `run.bat`
- **Mac**: double-click `run.command`

A terminal window opens (leave it open) and your browser opens the app at
`http://127.0.0.1:8765`. Close the terminal window to stop the app. After an
update, start it again to see the changes.

## The monthly routine

1. **Upload the month's stock statement** — Reports page, *Upload a new
   month*: drag the `.xls`/`.xlsx` export in (or click to browse). Or save
   it into the `uploads/` folder and click **Rescan uploads folder**.
2. **Upload the item list from the same day** — Reports page, *Item code
   list*. It gives every item a permanent code, which is how the app
   recognises an item that's been renamed. Reports reminds you whenever the
   item list is older than the latest statement.
3. **Check Import health** at the top of Reports — a ✓ means nothing needs
   attention; otherwise each line says what's wrong.
4. **Work through Review** (the top-bar tab shows how many are waiting) —
   confirm or reject possible renamed items, as many at a time as you like.

The first file can span several months (e.g. an initial Apr–Jul export) as
a starting point; after that, one month at a time, in any order — earlier
months can be backfilled later.

## The pages

### Dashboard — what needs action today

- **Headline tiles** — total SKUs, brands, stock value ("₹1.43 Cr") and
  units on hand, each with its change since last month ("▼ ₹20.7 L vs Jul
  2026"). Click SKUs, value or units to see its month-by-month history.
- **Action lists** — every SKU sorted into one status (see *How the numbers
  are calculated*): Out of stock, Low stock, Dead stock, Overstock, plus
  Healthy and Returned. Click a row to switch lists; each has a search box,
  sortable columns and **Export CSV** (all rows, not just the 300 shown).
  **Download all lists (Excel)** saves every list in one workbook. Each
  status's **History** button shows how its count and value moved month by
  month. The Dead Stock list also breaks down by how long it's been dead
  (90–179 days, 180–364 days, 1–2 years, 2+ years).
- **Value segments** — every SKU on the shelf sorted by how much money is
  in it (tier A: the top 70% of stock value, B: the next 20%, C: the long
  tail) crossed with how it's moving (Fast / Slow / Non-moving) — nine
  tiles, each showing its share of stock value. A valuable item that's
  stopped selling needs very different attention from a cheap one; this
  separates them. Click a tile for a recommendation and its SKU list;
  **Download all (Excel)** saves the whole card, and each tile has
  **Export CSV**.
- A notice appears at the top if the latest import is more than 35 days
  old, so out-of-date figures don't pass for current ones.

Click any SKU or brand anywhere to open its detail panel: period-by-period
history, a trend chart, its value segment, and any earlier names it had.

### Trends

The deeper dive: leaderboards (top SKUs by value or sales, top brands by
value) and month-over-month charts once two or more months are imported.

### Reports

- **Import health** — last refresh; any gap between imported periods; files
  a rescan couldn't use (unreadable, duplicate period, superseded by a newer
  file); the item-list reminder; renamed items waiting for review; SKU names
  that appeared or vanished since the last import with no explanation; and
  impossible values in the source files (**data quality** — e.g. negative
  stock). The data-quality warning can be **dismissed**; it comes back by
  itself if a later import finds a different set of issues.
- **Upload a new month** and **Item code list** — the two upload boxes. A
  file put in the wrong one is told so.
- **Imported reports**, grouped by financial year, each with **Remove**.

### Review

Renamed items. The same item can appear under a new name from one month to
the next — tagged "(NON)" before being returned, prefixed "ZZ", marked
"(RECALL)", or respelled by the POS ("IODEX 8G" → "IODEX 8GM"). Keyed on
names alone, that would split one item's history in two: the old name
"vanishes", the new one looks brand new with no sales history.

When the app is **sure** two names are the same item, it joins them up
everywhere (dashboard, history, dead-stock age, search, exports) under the
item's latest name:

- the item list gives both names **the same code**, or
- they differ **only by a tag** — (NON), ZZ, (RECALL) — or
- they're **spelled the same** apart from spaces, punctuation, unit spelling
  ("G"/"GM") or word order, *and* the stock carried over exactly from one
  month to the next.

It is never sure from the names alone when a word or number differs: "DOLO
200MG" and "DOLO 800MG", or "GLOEYE TAB" and "GLOEYE PLUS TAB", are
different products. If stock carried over between two such names, the pair
waits under **To review** with the differing words highlighted — **Same
item — merge** or **Not the same**. A pair where a number was *replaced* (a
different strength, pack size or count, like "CLINDAC A GEL 20GM" → "30GM")
isn't listed at all.

Keyboard: ↑/↓ (or J/K) move, **M** same item, **N** not the same, **U**
undo. **Merged automatically** lists every automatic merge, each with **Not
the same item** to split it — that overrides everything, item codes
included. **Your decisions** lists every decision with **Undo**. Decisions
are saved, so a long list can be worked through over several days. Nothing
in your imported reports is changed.

### Settings

The thresholds behind the statuses — low stock, overstock and dead stock
days, the sales-pace window — and the two value-tier percentages. Changes
apply immediately, everywhere. Low stock must be fewer days than overstock.

### Search (top of every page)

Finds any SKU or brand in the current stock, including healthy items and
items by a name they no longer go by.

## Fixing a bad upload

- **Wrong data in an imported file** — re-upload the corrected file with the
  same name (or overwrite it in `uploads/` and rescan). It replaces that
  report; nothing is duplicated.
- **A report that shouldn't exist** (wrong store, wrong period) — **Remove**
  it on Reports. If its file is still in `uploads/`, the next rescan imports
  it again — delete or fix the file too (the confirmation says so).
- **Two files for the same period** — can't happen silently: the second is
  rejected with a message instead of double-counting.
- **Two files whose dates overlap** (e.g. a mid-month export for Aug 1–9 and
  a later one for Aug 1–17) — the one saved most recently is used; the other
  shows in Import health as "superseded" and is left alone after that.
- **A filename now holding a different, unrelated period** — rejected with a
  message to remove the old report first, rather than leaving it orphaned.

## How the numbers are calculated

The thresholds below are the built-in defaults; all are editable in
Settings.

- **Current stock** (closing stock and value) comes from the most recently
  imported report.
- **Sales pace** — total demand (sales plus free/scheme units) across the
  most recent reports going back at least 90 days, divided by those days.
  **Days of cover** is current stock divided by that daily pace. Recomputed
  every time, so the window slides forward as months are imported.
- **Out of stock** — nothing on hand.
- **Returned** — nothing on hand, nothing sold since the item got its
  current name, and stock went back out as a return. A deliberate send-back,
  not a sell-out — so it isn't flagged for restocking.
- **Dead stock** — stock on hand, and at least 90 days since the later of
  its last restock or last sale. "Restocked" means stock actually arrived: a
  purchase, free/scheme units, or a transfer in that's larger than what went
  back out the same month (an exchange — 5 in, the same 5 out — doesn't
  count). A single old sale doesn't make an item permanently "alive".
- **Low stock** — selling, with under 15 days of cover.
- **Overstock** — over 90 days of cover, *and* the product has been around
  for at least those 90 days (counted from the end of the first report it
  appears in). A product first stocked last month isn't called overstock
  just for being new. Anything already in your earliest report, or first
  appearing with stock carried in, counts as established.
- **Healthy** — everything else.
- **Value segments** — SKUs on the shelf sorted by value, highest first;
  tier A covers whatever it takes to reach 70% of total stock value, B
  continues to 90%, C is the rest. Movement comes from the status: dead
  stock → Non-moving, overstock → Slow, low stock/healthy → Fast (a product
  too new to be called overstock, but with that much cover, still counts as
  Slow). "Value" is the stock statement's own Value column, which tracks
  purchase cost far more closely than MRP — so the tiers read as capital
  tied up.
- **History charts** re-run the same calculation as of each past month,
  with *today's* Settings — so changing a threshold redraws the whole
  history. The first months after your earliest import are marked "≈":
  there wasn't yet enough history to measure sales pace or dead stock.
- **Since last month** on the tiles compares with the last report ending in
  an earlier calendar month.
- **Data quality** — impossible combinations (positive stock with negative
  value, negative stock or sales, opening + received − issued not matching
  closing) are listed, never corrected — there's no way to know the true
  number — so a bad export can't silently skew a total.
- **Blind spot** — only the latest report says what's on the shelf. An item
  that had stock earlier but is simply missing from the latest report
  (rather than listed with zero) doesn't appear anywhere. This can happen at
  a financial-year rollover; there's no automatic detection yet.

## Where files go

- `uploads/<financial year>/` — every stock statement you upload, filed by
  financial year (`2526/` = Apr 2025–Mar 2026), kept as uploaded. A file
  named in a non-Latin script is saved under a generated name
  (`upload_<code>.xls`).
- `uploads/item_catalog.xlsx` — the current item list.
- `uploads/item_lists/<financial year>/item_list_<date>.xlsx` — a dated
  copy of each item list, **last six months only** (older copies are
  deleted automatically; the renames they revealed stay recorded in the
  database). Dropping a newer item list into `uploads/` and clicking Rescan
  imports it; an older one never replaces a newer one.
- `db/inventory.db` — the database: everything imported, settings, review
  decisions.
- `logs/error.log` — technical details if something goes wrong; send this
  file to whoever set up the app.

## Command line (optional, for automation)

```bash
uv run main.py refresh --open      # rescan uploads/, import anything new, build the HTML dashboard
uv run main.py import <file.xls>   # import one file from anywhere (--replace to overwrite its period)
uv run main.py list                # what's been imported, and any coverage gaps
uv run main.py remove <id>         # delete a report
uv run main.py dashboard --excel   # build output/inventory_dashboard.html + output/inventory_lists.xlsx
```

`dashboard`/`refresh` use the thresholds saved in Settings. `--low-stock-days`,
`--overstock-days`, `--trailing-days` and `--dead-stock-days` override them
for that run only.

## For developers

```bash
uv run pytest        # the test suite
uv run ruff check .  # lint
uv run app.py        # run the web app
```

Every change ships as its own branch merged with `--no-ff` and tagged
`merged/<name>`, so any one can be recalled with `git revert -m 1 <merge>`
while the rest keep working. `FEATURES.md` lists every merge, its revert
command, revert-order dependencies, and any database change.

```
app.py                  web app entry point (run.bat / run.command call this)
main.py                 command-line entry point
src/gowri_proj/
  webapp.py             create_app(): config, CSRF check, error pages, wiring
  datacache.py          DataCache: the cached, fingerprinted view of the database every route reads
  web_helpers.py        upload staging + Windows file-lock retries, CSV responses, small API helpers
  routes/               one module per area, each with register(app, data):
    pages.py              Dashboard, Trends, Reports, Review pages; favicon
    settings.py           Settings page and saving it
    inventory.py          search, SKU/brand detail, month-by-month history
    review.py             renamed-items review: list, decide, undo, export
    imports.py            uploads, rescan, remove, import health and its exports
    exports.py            Excel/CSV downloads of action lists and value segments
  templates/            Jinja pages; base.html holds shared JS (search, detail panels,
                        charts, date formatting) used on every page
  parser.py             reads the stock statement and item list exports
  db.py                 SQLite schema and queries
  sync.py               rescan of uploads/: statements, item lists, the item-list archive
  identity.py           which names are the same item (renamed-items rules)
  analysis.py           statuses, sales pace, dead stock, value segments, history, churn, gaps
  dashboard.py          the Dashboard's data payload (shared with the CLI export)
  dashboard_template.html  standalone HTML dashboard for the CLI (no server; kept separately)
  excel_export.py       Excel workbooks
  update_check.py       "update available" check against the git remote
tests/                  pytest suite
```
