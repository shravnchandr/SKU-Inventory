"""Inventory metrics computed across every imported stock statement.

Each import ("report") covers one period — ideally one calendar month, going
forward, though the very first import may span several months at once. This
module combines all imported reports into:

- a **current snapshot**: closing stock and value from the most recently
  imported report (i.e. what's on the shelf right now) — the only figures
  that are ever a single month's, since "right now" has no other meaning
- a **trailing window**: opening stock, purchases, and sales, all summed/
  anchored across the most recent reports together, covering at least
  TRAILING_DAYS_TARGET days (falling back to all imported history if there
  isn't that much yet). Sales here is what "days of cover" is measured
  against, so it gets more precise as more months are imported. Opening
  and purchases share the same window deliberately — showing them from a
  single month next to several months of sales would make the numbers look
  inconsistent even though each is individually correct
- a **trend series**: one data point per imported report, for charting how
  stock value and sales have moved over time

Every SKU is bucketed into one status based on its current stock and its
sales activity:

- ``out_of_stock``  closing stock is zero (or negative)
- ``dead_stock``    stock on hand, and nothing bought or sold for at least
                     DEAD_STOCK_DAYS (searched within a bounded lookback
                     window, not the complete imported history — see
                     AGING_LOOKBACK_FLOOR_DAYS/_compute_dead_stock — so this
                     stays fast as more months pile up over the years)
- ``low_stock``     selling, but less than LOW_STOCK_DAYS of cover left
- ``overstock``     selling, but more than OVERSTOCK_DAYS of cover on hand
- ``healthy``       everything else
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

LOW_STOCK_DAYS = 15
OVERSTOCK_DAYS = 90
TRAILING_DAYS_TARGET = 90  # how much recent history to average sales pace over
DEAD_STOCK_DAYS = 90  # "hasn't sold or been restocked in 3+ months" — see _compute_dead_stock

# Valid range for the four threshold settings above — enforced identically
# by the web Settings page (webapp.py) and the CLI flags (main.py), so both
# reject e.g. a 0-day trailing window (which would divide by zero) the same way.
THRESHOLD_DAYS_MIN = 1
THRESHOLD_DAYS_MAX = 3650

# Floor for how far back _compute_dead_stock searches for a SKU's last
# purchase/sale — covers the aging buckets' largest finite boundary (730
# days / 2 years, see DEAD_STOCK_AGING_BUCKETS below) with margin, so a SKU
# whose true last activity is (say) 5 years ago is still correctly bucketed
# as "730+", without the search actually needing to scan 5 years of rows to
# find that out. Without this floor, dead-stock detection would re-scan
# every row of every ever-imported report on every dashboard rebuild — the
# single biggest cost in this whole module once a few years of monthly
# history pile up (empirically ~75% of total compute time at 10+ years).
AGING_LOOKBACK_FLOOR_DAYS = 750


def _period_label(period_start: date, period_end: date) -> str:
    if period_start.year == period_end.year and period_start.month == period_end.month:
        return period_start.strftime("%b %Y")
    return f"{period_start.strftime('%b %Y')}–{period_end.strftime('%b %Y')}"


# Canonical status list. dashboard.py, main.py, and excel_export.py all
# import it rather than repeating the literal. Priority order for the
# first five matches _status()'s if/elif chain below; "returned" is a
# special case — same priority as out_of_stock for *classification*
# purposes (see _status docstring), but listed last here since, unlike the
# other five, it's purely informational (aggregate count/value only, no
# per-SKU table) and displayed grouped with "healthy" rather than among
# the actionable buckets.
STATUS_ORDER = ["out_of_stock", "low_stock", "dead_stock", "overstock", "healthy", "returned"]


def format_days_of_cover(value: float) -> float | None:
    """days_of_cover is `inf` for a SKU with no sales in the trailing window
    (nothing to divide by) — not valid JSON, so every place that serializes
    it for the API/UI needs this same None-if-infinite conversion."""
    return None if value == float("inf") else round(float(value), 1)


def _status(
    closing_stock: float,
    is_dead: bool,
    days_of_cover: float,
    is_returned: bool = False,
    low_stock_days: int = LOW_STOCK_DAYS,
    overstock_days: int = OVERSTOCK_DAYS,
) -> str:
    if closing_stock <= 0:
        # Zero stock with nothing sold, but stock actually left (a return/
        # write-off, not a sale) in the trailing window — a deliberate call
        # not to reorder, not the same signal as "sold through, might need
        # restocking." See is_returned's computation in summarize_history.
        return "returned" if is_returned else "out_of_stock"
    if is_dead:
        return "dead_stock"
    if days_of_cover < low_stock_days:
        return "low_stock"
    if days_of_cover > overstock_days:
        return "overstock"
    return "healthy"


def _compute_dead_stock(
    all_entries: pd.DataFrame,
    current_skus: pd.DataFrame,
    latest_period_end: pd.Timestamp,
    earliest_period_start: pd.Timestamp,
    dead_stock_days: int,
) -> pd.DataFrame:
    """For every SKU in ``current_skus``: is it dead stock?

    Anchored to whichever is more recent — the last time it was *purchased*
    or the last time it *sold* — not a fixed trailing window shared by every
    SKU, and not "purchase" alone (a single sale right after a purchase two
    years ago shouldn't grant permanent immunity from ever being flagged
    dead again; the clock has to reset on whatever happened most recently,
    restock or sale). A SKU restocked last month that hasn't sold yet isn't
    dead; one restocked 4 months ago that still hasn't moved is; one that
    sold a handful of units right after being restocked a year ago but
    nothing since is dead once enough time has passed since *that* sale.

    Keyed on ``sku`` alone, not ``(brand, sku)`` — the source export's brand
    label for a SKU can (and, checked against real data, does) change over
    time, e.g. a distributor renaming a division. Keying on the pair would
    silently split one physical product's purchase/sale history in two at
    the rename boundary, making a SKU that's actually moving fine look dead
    just because its *old* label's activity isn't visible under the *new*
    one. Confirmed safe on real data: no SKU has ever appeared under two
    different brand labels within the *same* report period — every
    multi-brand SKU is a rename over time, never a genuine simultaneous
    collision between two unrelated products sharing a SKU string.

    Two things keep this bounded by *current catalog size*, not by however
    many months have ever been imported (see AGING_LOOKBACK_FLOOR_DAYS):
    it only computes for SKUs that exist *now* (not every SKU that ever
    appeared in any historical report), and it only searches a rolling
    lookback window for the last purchase/sale rather than all of history —
    once nothing's happened to a SKU in that whole window, we know it's
    "dead" and roughly how dead (which aging bucket), without needing to
    know the *exact* day it was last touched.
    """
    lookback_days = max(dead_stock_days, AGING_LOOKBACK_FLOOR_DAYS)
    lookback_cutoff = latest_period_end - pd.Timedelta(days=lookback_days)
    recent = all_entries[all_entries["period_end"] >= lookback_cutoff]

    last_purchase_end = recent[recent["purchase"] > 0].groupby("sku")["period_end"].max()
    last_sale_end = recent[recent["sales"] > 0].groupby("sku")["period_end"].max()

    result = current_skus[["sku"]].drop_duplicates().set_index("sku")
    result["last_purchase_end"] = last_purchase_end
    result["last_sale_end"] = last_sale_end
    result["last_activity_end"] = result[["last_purchase_end", "last_sale_end"]].max(axis=1)
    # Nothing found within the lookback window — it's dead, at least
    # `lookback_days` old, whether the true last activity was just outside
    # the window or never happened at all; which one doesn't matter for
    # either the is_dead check or which aging bucket it lands in.
    #
    # BUT lookback_cutoff can fall before the earliest report we've ever
    # imported (e.g. a fresh install with 16 months of history and a
    # 750-day lookback floor) — in that case "nothing in the window" really
    # means "nothing in the SKU's entire recorded life," and the honest
    # last-known-activity date is when we first started tracking it, not
    # the fictional pre-history cutoff. Using lookback_cutoff there would
    # fabricate an age the data can't support (e.g. bucketing a SKU as
    # "2+ years dead" when it's only been tracked for 16 months).
    fallback = max(lookback_cutoff, earliest_period_start)
    result["last_activity_end"] = result["last_activity_end"].fillna(fallback)

    result["days_since_activity"] = (latest_period_end - result["last_activity_end"]).dt.days
    result["is_dead"] = result["days_since_activity"] >= dead_stock_days
    return result[["is_dead", "days_since_activity"]].reset_index()


DEAD_STOCK_AGING_BUCKETS = [
    ("90-179", 90, 180),
    ("180-364", 180, 365),
    ("365-729", 365, 730),
    ("730+", 730, None),  # 2 years+
]


def _dead_stock_aging(dead_stock: pd.DataFrame) -> list:
    """Bucket the dead-stock list by how long it's actually been dead.

    Returns a list (not a dict) in youngest-to-oldest bucket order — a dict
    would look right in Python, but round-tripping through JSON for the
    webapp is free to reorder its keys (Flask's ``tojson`` filter
    alphabetizes them by default), which is exactly what scrambled the tile
    order in the UI. A list's order can't be reshuffled by serialization.
    """
    aging = []
    for label, low, high in DEAD_STOCK_AGING_BUCKETS:
        if high is None:
            bucket = dead_stock[dead_stock["days_since_activity"] >= low]
        else:
            bucket = dead_stock[
                (dead_stock["days_since_activity"] >= low) & (dead_stock["days_since_activity"] < high)
            ]
        aging.append({"bucket": label, "count": int(len(bucket)), "value": round(float(bucket["value"].sum()), 2)})
    return aging


def _select_trailing_reports(reports: pd.DataFrame, trailing_days_target: int) -> pd.DataFrame:
    """Most recent reports whose combined period_days reaches the target (or all of them)."""
    ordered = reports.sort_values("period_end", ascending=False)
    running = 0
    ids = []
    for _, r in ordered.iterrows():
        if running >= trailing_days_target:
            break
        ids.append(r["report_id"])
        running += int(r["period_days"])
    return reports[reports["report_id"].isin(ids)]


@dataclass
class HistoryMeta:
    company: str | None
    location: str | None
    earliest_period_start: date
    latest_period_end: date
    report_count: int
    trailing_days: int  # actual days of history the sales rate is based on


@dataclass
class TrendSeries:
    labels: list[str]
    period_ends: list[str]
    inventory_value: list[float]
    units_sold: list[float]
    top_brands: list[str]
    brand_units_sold: dict[str, list[float]]  # brand -> per-period units sold


@dataclass
class InventorySummary:
    meta: HistoryMeta
    total_skus: int
    total_brands: int
    total_value: float
    total_units: float
    status_counts: dict[str, int]
    status_values: dict[str, float]
    top_brands_by_value: pd.DataFrame
    top_skus_by_value: pd.DataFrame
    top_skus_by_sales: pd.DataFrame
    out_of_stock: pd.DataFrame
    low_stock: pd.DataFrame
    dead_stock: pd.DataFrame
    overstock: pd.DataFrame
    dead_stock_aging: list
    trend: TrendSeries
    enriched: pd.DataFrame = field(repr=False)


def summarize_history(
    all_entries: pd.DataFrame,
    trailing_days_target: int = TRAILING_DAYS_TARGET,
    dead_stock_days: int = DEAD_STOCK_DAYS,
    low_stock_days: int = LOW_STOCK_DAYS,
    overstock_days: int = OVERSTOCK_DAYS,
) -> InventorySummary:
    """Build the full inventory summary from every imported report.

    ``all_entries`` is the output of ``db.load_all_entries``: one row per
    (report, SKU), with report period columns joined in.
    """
    reports = (
        all_entries[["report_id", "period_start", "period_end", "period_days", "company", "location"]]
        .drop_duplicates("report_id")
        # Tiebroken on period_start too, not just period_end — the schema
        # only enforces uniqueness on the (period_start, period_end) pair,
        # not on period_end alone, and all_entries is deliberately fetched
        # unordered from SQL for performance (see db.load_all_entries), so
        # without this, which report counts as "latest" when two share a
        # period_end would be non-deterministic across runs.
        .sort_values(["period_end", "period_start"])
        .reset_index(drop=True)
    )
    latest = reports.iloc[-1]
    latest_report_id = latest["report_id"]

    # --- trailing window: opening/purchased/sold, all from the same span ---
    # Grouped by sku alone, not (brand, sku) — see _compute_dead_stock's
    # docstring for why: a brand-label rename mid-window would otherwise
    # silently drop the activity recorded under the old label.
    #
    # Opening/Purchased share Sold's window rather than just the latest
    # month (see the module docstring for why) — concretely, that's the
    # difference between "opening 19, purchased 10, sold 137" looking
    # broken vs. "opening 10, purchased 128, sold 137" balancing to the
    # actual closing stock.
    trailing_reports = _select_trailing_reports(reports, trailing_days_target)
    trailing_days = int(trailing_reports["period_days"].sum())
    trailing_entries = all_entries[all_entries["report_id"].isin(trailing_reports["report_id"])]

    # --- current snapshot: what's on the shelf right now ---
    # Sliced from trailing_entries, not all_entries — _select_trailing_reports
    # always includes the latest report (it starts accumulating from there),
    # so the latest report's rows are already a subset of trailing_entries.
    # Scanning that instead of the full imported history keeps this bounded
    # by the trailing window rather than growing with years of history.
    # Only closing_stock/value here — opening_stock/purchase come from the
    # trailing aggregate below instead.
    current = trailing_entries[trailing_entries["report_id"] == latest_report_id][
        ["brand", "sku", "closing_stock", "value"]
    ].copy()

    trailing_agg = (
        trailing_entries.assign(
            # "Purchased" means "stock that arrived," not just the paid-purchase
            # column — purchase_free (scheme/bonus units) and other_receipt
            # (e.g. inter-branch transfers, corrections) are real incoming
            # stock too. Leaving them out understated how much actually came
            # in, which could make a large other_receipt-fed SKU look like it
            # returned more than it ever had (opening + purchase alone) —
            # it didn't; the stock arrived through a channel this column
            # wasn't counting. Sales_free/other_issue stay separate: they
            # don't feed "Sold" (that number specifically drives the sales
            # pace behind days-of-cover/dead-stock, and folding in giveaways
            # or write-offs would change classifications, not just display).
            total_purchase=trailing_entries["purchase"]
            + trailing_entries["purchase_free"]
            + trailing_entries["other_receipt"]
        )
        .sort_values("period_end")
        .groupby("sku")
        .agg(
            trailing_opening=("opening_stock", "first"),
            trailing_purchase=("total_purchase", "sum"),
            trailing_sales=("sales", "sum"),
            trailing_other_issue=("other_issue", "sum"),
        )
        .reset_index()
    )

    # --- dead stock: no sales since last purchase, independent of the trailing window above ---
    dead_flags = _compute_dead_stock(
        all_entries, current, latest["period_end"], reports.iloc[0]["period_start"], dead_stock_days
    )

    merged = current.merge(trailing_agg, on="sku", how="left")
    # Every SKU in `current` comes from the latest report, which is always
    # included in the trailing window by construction — so these fillna
    # calls are a defensive fallback, not something normal data should hit.
    merged["trailing_opening"] = merged["trailing_opening"].fillna(0.0)
    merged["trailing_purchase"] = merged["trailing_purchase"].fillna(0.0)
    merged["trailing_sales"] = merged["trailing_sales"].fillna(0.0)
    merged["trailing_other_issue"] = merged["trailing_other_issue"].fillna(0.0)
    merged = merged.merge(dead_flags, on="sku", how="left")
    merged["is_dead"] = merged["is_dead"].fillna(False)
    merged["days_since_activity"] = merged["days_since_activity"].fillna(0).astype(int)
    merged["daily_sales"] = merged["trailing_sales"] / trailing_days if trailing_days > 0 else 0.0
    # Zero stock, nothing sold, but stock actually left via a return/write-off
    # (other_issue) somewhere in the trailing window — this SKU wasn't sold
    # through, it was deliberately sent back because it wasn't moving. That's
    # a different signal than "sold out, might need restocking" (out_of_stock)
    # even though both end up at zero on the shelf — see _status()'s docstring.
    merged["is_returned"] = (merged["trailing_sales"] == 0) & (merged["trailing_other_issue"] > 0)
    # Vectorized rather than merged.apply(..., axis=1) — a row-wise Python
    # loop over every current SKU (15k+ today) was the remaining hot spot
    # in this module once _compute_dead_stock got bounded (see
    # AGING_LOOKBACK_FLOOR_DAYS). np.select mirrors _status()'s if/elif
    # chain exactly: conditions are checked in the same priority order,
    # first match wins — keep the two in sync if either changes.
    merged["days_of_cover"] = np.where(
        merged["daily_sales"] > 0, merged["closing_stock"] / merged["daily_sales"], float("inf")
    )
    merged["status"] = np.select(
        [
            (merged["closing_stock"] <= 0) & merged["is_returned"],
            merged["closing_stock"] <= 0,
            merged["is_dead"],
            merged["days_of_cover"] < low_stock_days,
            merged["days_of_cover"] > overstock_days,
        ],
        ["returned", "out_of_stock", "dead_stock", "low_stock", "overstock"],
        default="healthy",
    )
    # Aliased to the plain names downstream table/column code already
    # expects (dead-stock classification uses its own since-purchase window
    # internally, above — unaffected by this).
    merged = merged.rename(
        columns={
            "trailing_sales": "sales",
            "trailing_opening": "opening_stock",
            "trailing_purchase": "purchase",
            "trailing_other_issue": "other_issue",
        }
    )

    status_counts = merged["status"].value_counts().to_dict()
    status_values = merged.groupby("status")["value"].sum().to_dict()

    top_brands_by_value = (
        merged.groupby("brand")["value"].sum().sort_values(ascending=False).head(10).reset_index()
    )
    top_skus_by_value = merged.sort_values("value", ascending=False).head(15)[
        ["brand", "sku", "closing_stock", "value"]
    ]
    top_skus_by_sales = merged.sort_values("sales", ascending=False).head(15)[
        ["brand", "sku", "sales", "value"]
    ]

    def subset(status: str, sort_col: str, extra_cols: list[str] | None = None) -> pd.DataFrame:
        cols = ["brand", "sku", "opening_stock", "purchase", "sales", "closing_stock", "value", "days_of_cover"]
        cols = cols + (extra_cols or [])
        s = merged[merged["status"] == status][cols]
        return s.sort_values(sort_col, ascending=False)

    dead_stock_df = subset("dead_stock", "value", extra_cols=["days_since_activity"])
    dead_stock_aging = _dead_stock_aging(dead_stock_df)

    # --- trend series, one point per imported report ---
    per_report = all_entries.groupby("report_id").agg(value=("value", "sum"), sales=("sales", "sum"))
    reports_ordered = reports.merge(per_report, on="report_id")
    labels = [
        _period_label(r["period_start"].date(), r["period_end"].date())
        for _, r in reports_ordered.iterrows()
    ]
    top_brand_names = list(top_brands_by_value["brand"].head(5))
    # One pass over all_entries for all 5 brands together, rather than one
    # full-table scan per brand — matters once history spans years of
    # imported reports.
    by_brand_report = (
        all_entries[all_entries["brand"].isin(top_brand_names)]
        .groupby(["brand", "report_id"])["sales"]
        .sum()
    )
    brand_units_sold: dict[str, list[float]] = {
        brand: [float(by_brand_report.get((brand, rid), 0.0)) for rid in reports_ordered["report_id"]]
        for brand in top_brand_names
    }

    trend = TrendSeries(
        labels=labels,
        period_ends=[r["period_end"].date().isoformat() for _, r in reports_ordered.iterrows()],
        inventory_value=[round(float(v), 2) for v in reports_ordered["value"]],
        units_sold=[round(float(v), 2) for v in reports_ordered["sales"]],
        top_brands=top_brand_names,
        brand_units_sold=brand_units_sold,
    )

    meta = HistoryMeta(
        company=latest["company"],
        location=latest["location"],
        earliest_period_start=reports.iloc[0]["period_start"].date(),
        latest_period_end=latest["period_end"].date(),
        report_count=len(reports),
        trailing_days=trailing_days,
    )

    return InventorySummary(
        meta=meta,
        total_skus=len(merged),
        total_brands=merged["brand"].nunique(),
        total_value=float(merged["value"].sum()),
        total_units=float(merged["closing_stock"].sum()),
        status_counts=status_counts,
        status_values=status_values,
        top_brands_by_value=top_brands_by_value,
        top_skus_by_value=top_skus_by_value,
        top_skus_by_sales=top_skus_by_sales,
        out_of_stock=subset("out_of_stock", "value"),
        low_stock=subset("low_stock", "days_of_cover"),
        dead_stock=dead_stock_df,
        overstock=subset("overstock", "value"),
        dead_stock_aging=dead_stock_aging,
        trend=trend,
        enriched=merged,
    )


def search_skus(enriched: pd.DataFrame, query: str, limit: int = 50) -> list[dict]:
    """Case-insensitive substring search across every *current* SKU — brand
    or name — not scoped to any one status bucket. Unlike the per-tab search
    boxes on the dashboard (which only filter within whichever action-list
    table is already embedded on the page — out-of-stock/low-stock/
    dead-stock/overstock), this covers every SKU including "healthy" ones,
    which aren't embedded anywhere on the page at all.
    """
    q = query.strip().lower()
    if len(q) < 2:
        return []
    mask = enriched["brand"].str.lower().str.contains(q, na=False, regex=False) | enriched["sku"].str.lower().str.contains(
        q, na=False, regex=False
    )
    matches = enriched[mask].sort_values("value", ascending=False).head(limit)
    return [
        {
            "brand": r["brand"],
            "sku": r["sku"],
            "status": r["status"],
            "closing_stock": float(r["closing_stock"]),
            "value": round(float(r["value"]), 2),
            "days_of_cover": format_days_of_cover(r["days_of_cover"]),
        }
        for _, r in matches.iterrows()
    ]



def _pair_renames_via_catalog(
    vanished_rows: dict[str, dict], new_rows: dict[str, dict], alias_map: dict[str, str]
) -> tuple[list[dict], set[str], set[str]]:
    """Pair a vanished name with a new name only when the item catalog has
    recorded an actual code-anchored rename between exactly those two names
    (see db.get_name_change_map) — not a text-similarity guess. Same code
    means definitely the same item, so this never merges two genuinely
    different products (e.g. two different strengths of the same drug) the
    way a similarity score alone could.

    This means a rename only gets recognized once the item list has been
    re-uploaded *after* the POS system's own rename — until then, "XYZ 10G"
    and "XYZ 10GM" show up as an unresolved vanished/new pair, same as any
    other churn. That's expected, not a bug: there's no code in the monthly
    stock statement itself to resolve it any earlier.

    Returns (likely_pairs, paired_vanished_names, paired_new_names).
    """
    paired_vanished: set[str] = set()
    paired_new: set[str] = set()
    pairs = []
    for v_name, v_row in vanished_rows.items():
        n_name = alias_map.get(v_name)
        if n_name is None or n_name not in new_rows or n_name in paired_new:
            continue
        paired_vanished.add(v_name)
        paired_new.add(n_name)
        pairs.append(
            {
                "brand": new_rows[n_name]["brand"],
                "old_name": v_name,
                "new_name": n_name,
                "closing_stock": new_rows[n_name]["closing_stock"],
                "value": new_rows[n_name]["value"],
            }
        )
    pairs.sort(key=lambda p: -p["value"])
    return pairs, paired_vanished, paired_new


def find_sku_churn(
    all_entries: pd.DataFrame, alias_map: dict[str, str] | None = None, limit: int = 50
) -> dict:
    """SKU names that appeared or disappeared between the two most recently
    imported reports — after pairing off names the item catalog confirms are
    the same code renamed (see _pair_renames_via_catalog), so a "(NON)" tag
    toggling or a unit respelled after a fresh item-list upload doesn't read
    as "1 new + 1 vanished" forever.

    What's left after pairing is the actual trigger for "go check your item
    list": a genuinely new or discontinued-looking name, unrelated to
    anything on the other side, shows up here directly — without needing
    the catalog at all — the catalog only helps *resolve* it once flagged.
    Deliberately scoped to the latest two reports, not all-time history:
    churn from three years ago was already noticed (or wasn't) three years
    ago, and re-flagging it forever would just be noise.
    """
    empty = {
        "new_skus": [], "new_total": 0,
        "vanished_skus": [], "vanished_total": 0,
        "likely_renames": [], "renames_total": 0,
        "previous_period_end": None,
    }
    if all_entries.empty:
        return empty

    period_ends = all_entries[["report_id", "period_end"]].drop_duplicates().sort_values("period_end")
    if len(period_ends) < 2:
        return empty

    latest_id, previous_id = period_ends.iloc[-1]["report_id"], period_ends.iloc[-2]["report_id"]
    latest = all_entries[all_entries["report_id"] == latest_id]
    previous = all_entries[all_entries["report_id"] == previous_id]

    latest_skus = set(latest["sku"])
    previous_skus = set(previous["sku"])
    new_names = latest_skus - previous_skus
    vanished_names = previous_skus - latest_skus

    def _row_map(df: pd.DataFrame, names: set[str]) -> dict[str, dict]:
        subset = df[df["sku"].isin(names)]
        return {
            r["sku"]: {
                "brand": r["brand"],
                "closing_stock": float(r["closing_stock"]),
                "value": round(float(r["value"]), 2),
            }
            for _, r in subset.iterrows()
        }

    new_rows = _row_map(latest, new_names)
    vanished_rows = _row_map(previous, vanished_names)
    rename_pairs, paired_vanished, paired_new = _pair_renames_via_catalog(
        vanished_rows, new_rows, alias_map or {}
    )

    def _rows(rows: dict[str, dict], names: set[str]) -> list[dict]:
        items = sorted(
            ({"sku": name, **rows[name]} for name in names), key=lambda r: -r["value"]
        )
        return items[:limit]

    true_new = new_names - paired_new
    true_vanished = vanished_names - paired_vanished
    return {
        "new_skus": _rows(new_rows, true_new),
        "new_total": len(true_new),
        "vanished_skus": _rows(vanished_rows, true_vanished),
        "vanished_total": len(true_vanished),
        "likely_renames": rename_pairs[:limit],
        "renames_total": len(rename_pairs),
        "previous_period_end": period_ends.iloc[-2]["period_end"].date().isoformat(),
    }


def find_unmatched_skus(enriched: pd.DataFrame, catalog_names: set[str], limit: int = 100) -> dict:
    """Currently-stocked SKUs whose name doesn't appear anywhere in the item
    catalog (product_name or long_name, exact match after stripping) —
    scoped to the *current* snapshot only, same as every other action list
    in this app. A non-empty result means either the item list export is
    stale (re-export it from the POS system) or the item genuinely isn't in
    the master catalog yet — either way, it's a name the catalog can't
    vouch for.

    Returns ``{"total": N, "items": [...]}`` — ``total`` is the *true* count
    (so the UI can say "632 SKUs don't match" even when only the top `limit`,
    by value, are actually listed) and ``items`` is that display-capped list.
    """
    if not catalog_names or enriched.empty:
        return {"total": 0, "items": []}
    current_names = enriched["sku"].str.strip()
    mask = ~current_names.isin(catalog_names)
    unmatched = enriched[mask].sort_values("value", ascending=False)
    items = [
        {
            "brand": r["brand"],
            "sku": r["sku"],
            "closing_stock": float(r["closing_stock"]),
            "value": round(float(r["value"]), 2),
        }
        for _, r in unmatched.head(limit).iterrows()
    ]
    return {"total": int(mask.sum()), "items": items}


def find_import_gaps(reports: pd.DataFrame) -> dict:
    """Look for gaps in monthly coverage across every imported report.

    ``reports`` needs ``period_start``/``period_end`` columns (e.g.
    ``db.list_reports()``'s output). Returns two views of the same question
    ("is there a stretch of time nothing was imported for?"):

    - ``missing_months`` — the coarse, calendar-month headline: a month with
      *zero* reports overlapping it at all (a report counts as covering a
      month if its period overlaps that month at all, not just if it starts
      there — so one wide report spanning several months correctly covers
      all of them).
    - ``coverage_gaps`` — the precise day-level view: the gap between when
      one report's period ends and the next one's begins. This is what
      actually catches a boundary gap that doesn't line up with whole
      calendar months — exactly the shape of the real incident that
      motivated this function (193 SKUs' worth of stock going untracked
      across a financial-year rollover).
    """
    if reports.empty:
        return {"missing_months": [], "coverage_gaps": []}

    ordered = reports.sort_values("period_start").reset_index(drop=True)
    starts = pd.to_datetime(ordered["period_start"])
    ends = pd.to_datetime(ordered["period_end"])

    coverage_gaps = []
    covered_until = ends.iloc[0]
    for i in range(1, len(ordered)):
        if starts.iloc[i] > covered_until + pd.Timedelta(days=1):
            gap_start = covered_until + pd.Timedelta(days=1)
            gap_end = starts.iloc[i] - pd.Timedelta(days=1)
            coverage_gaps.append(
                {
                    "gap_start": gap_start.date().isoformat(),
                    "gap_end": gap_end.date().isoformat(),
                    "days": (gap_end - gap_start).days + 1,
                }
            )
        covered_until = max(covered_until, ends.iloc[i])

    months = pd.period_range(starts.min().to_period("M"), ends.max().to_period("M"), freq="M")
    missing_months = []
    for m in months:
        month_start, month_end = m.start_time, m.end_time
        overlaps = ((starts <= month_end) & (ends >= month_start)).any()
        if not overlaps:
            missing_months.append(month_start.strftime("%b %Y"))

    return {"missing_months": missing_months, "coverage_gaps": coverage_gaps}


def sku_history(all_entries: pd.DataFrame, sku: str) -> list[dict]:
    """Every imported period's figures for one specific SKU, oldest first.

    Matched on ``sku`` alone, not brand — a SKU's brand label can change
    over time (see _compute_dead_stock's docstring), and this should still
    show its full history across that rename, not just what happened under
    whichever brand label the caller passed in.
    """
    rows = all_entries[all_entries["sku"] == sku].sort_values("period_end")
    return [
        {
            "label": _period_label(r["period_start"].date(), r["period_end"].date()),
            "period_end": r["period_end"].date().isoformat(),
            "opening_stock": float(r["opening_stock"]),
            "purchase": float(r["purchase"]),
            "sales": float(r["sales"]),
            "closing_stock": float(r["closing_stock"]),
            "value": float(r["value"]),
        }
        for _, r in rows.iterrows()
    ]


def brand_history(all_entries: pd.DataFrame, brand: str) -> list[dict]:
    """Every imported period's totals for one specific brand, oldest first."""
    rows = all_entries[all_entries["brand"] == brand]
    per_report = rows.groupby("report_id").agg(
        value=("value", "sum"), sales=("sales", "sum"), period_start=("period_start", "first"),
        period_end=("period_end", "first"),
    ).sort_values("period_end")
    return [
        {
            "label": _period_label(r["period_start"].date(), r["period_end"].date()),
            "period_end": r["period_end"].date().isoformat(),
            "value": round(float(r["value"]), 2),
            "units_sold": round(float(r["sales"]), 2),
        }
        for _, r in per_report.iterrows()
    ]


# Rows that are logically impossible, not just unusual — worth surfacing so a
# bad source export doesn't silently skew totals or trend charts. This is
# purely informational: nothing here changes what gets imported or how it's
# classified, since we have no way to know the *correct* value, only that
# something's off. A tiny negative value very close to zero (floating-point
# noise from the source spreadsheet's own cost calculations) isn't flagged —
# only when the magnitude is large enough to plausibly matter.
_VALUE_NOISE_FLOOR = 0.01  # rupees


def _quality_checks(row: pd.Series) -> list[str]:
    issues = []
    if row["closing_stock"] > 0 and row["value"] < -_VALUE_NOISE_FLOOR:
        issues.append("Positive stock but negative value")
    if row["closing_stock"] < 0:
        issues.append("Negative closing stock")
    if row["opening_stock"] < 0:
        issues.append("Negative opening stock")
    if row["sales"] < 0:
        issues.append("Negative sales (possibly a return)")
    return issues


def find_data_quality_issues(all_entries: pd.DataFrame) -> list[dict]:
    """Scan every imported row for impossible combinations, most recent first."""
    candidates = all_entries[
        ((all_entries["closing_stock"] > 0) & (all_entries["value"] < -_VALUE_NOISE_FLOOR))
        | (all_entries["closing_stock"] < 0)
        | (all_entries["opening_stock"] < 0)
        | (all_entries["sales"] < 0)
    ]
    out = []
    for _, r in candidates.sort_values("period_end", ascending=False).iterrows():
        for issue in _quality_checks(r):
            out.append(
                {
                    "brand": r["brand"],
                    "sku": r["sku"],
                    "period_label": _period_label(r["period_start"].date(), r["period_end"].date()),
                    "period_end": r["period_end"].date().isoformat(),
                    "issue": issue,
                    "opening_stock": float(r["opening_stock"]),
                    "purchase": float(r["purchase"]),
                    "sales": float(r["sales"]),
                    "closing_stock": float(r["closing_stock"]),
                    "value": round(float(r["value"]), 2),
                }
            )
    return out
