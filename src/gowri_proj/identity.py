"""Which stock-statement names are the same physical item.

The monthly stock statement identifies an item by name only, and names drift:
the pharmacy tags non-moving stock "(NON)" before returning it (and may drop
the tag if it's sold again), prefixes discontinued items "ZZ", marks recalls
"(RECALL)", and the POS respells or reformats names (e.g. "IODEX 8G" became
"IODEX 8GM" at the 2026 financial-year rollover). Keyed on raw names, every
such change splits one item's history in two: the old name "vanishes", the
new one shows up as "new" with no sales history, and its dead-stock age,
sales pace and trend all restart from nothing.

This module groups names into items and rewrites each row's ``sku`` to one
display name per item (the name it carries in the most recent report), at
load time. Stored rows are never changed, so the grouping can be refined by
every new report, item list and review decision, and undone cleanly.

Evidence, strongest first:

1. **Item code** from the POS item list (uploaded monthly alongside the stock
   statement). Same code = same item; *different* codes = never the same
   item, whatever any other rule says. The item list truncates product names
   to 25 characters, so a longer stock-statement name is matched on its
   first 25. A code found only that way, or only through the item list's
   *long* name, is weaker evidence: two strengths can share their first 25
   characters, and the item list has codes whose short and long names name
   different variants ("P/A S/C CLASSIC" / "P/A S/C COOL BLUE"). Those merge
   automatically only when the spelling is identical (rule 3's test).
2. **Pharmacy tags** — names that are identical once "(NON)", "(RECALL)" and
   a leading "ZZ" are removed.
3. **Same spelling with stock carried over** — between consecutive reports,
   a name that vanished while holding stock and a brand-new name in the same
   brand whose opening stock equals that closing stock, *and* whose names
   are identical apart from spacing, punctuation, unit spelling (G/GM/GMS,
   'S/S) and word order.

Anything weaker — a word or number added, removed or changed — is never
merged automatically, because for medicines those differences *are* the
identity ("DOLO 200MG" vs "DOLO 800MG", "GLOEYE TAB" vs "GLOEYE PLUS TAB",
"TOPLAP CREAM" vs "TOPLAP GEL"). If stock was carried over between them it
becomes a *suggestion* that someone must approve, one by one — unless a
number *changed* ("CLINDAC A GEL 20GM" -> "30GM", "PAMP PREM L 48'S" ->
"44S"): a different strength, pack size or count is a different product,
so that's not even asked. (The POS did carry stock across those, at the
2026 financial-year rollover, but the pharmacy treats each pack size as its
own product.) A number only added or removed ("BIGEN … B102" -> "B102
40GM", a pack size that was missing) is still asked about.

People have the last word, in both directions (db.sku_merge_decisions,
stored so a long review can be worked through over several days):

- "merge" — approve a suggestion: these two names are the same item.
- "separate" — these two names are different items. Used both to reject a
  suggestion and to undo any automatic merge. It overrides every rule,
  item codes included, and holds for the *items*, not just the one link:
  the two names can't end up together through any third name either.

Deleting a decision (Undo) puts the pair back to whatever the rules say.

Two more guards apply to every automatic merge except item codes: two
names that both appear in the same report are different items (one report
never lists an item twice), and names with different known item codes are
never merged.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import pairwise

import pandas as pd

# The POS item list's product names are cut off at this many characters.
CATALOG_NAME_MAX = 25

# Pharmacy tags that don't change what the item is.
_TAGS = re.compile(r"\(\s*NON\s*\)|(?<![A-Z0-9])NON\)|\(\s*RECALL\s*\)", re.IGNORECASE)
_ZZ_PREFIX = re.compile(r"^\s*ZZ(?=\S)", re.IGNORECASE)

# Unit spellings that mean the same thing, applied after a number only.
_UNIT_SPELLINGS = [
    (re.compile(r"(\d)\s*(?:GMS|GM|G)\b"), r"\1G"),
    (re.compile(r"(\d)\s*(?:MLS|ML)\b"), r"\1ML"),
    (re.compile(r"(\d)\s*MG\b"), r"\1MG"),
    (re.compile(r"(\d)\s*'?\s*S\b"), r"\1S"),
]

REASON_CODE = "same item code"
REASON_TAG = "pharmacy tag"
REASON_SPELLING = "same spelling, stock carried over"
REASON_APPROVED = "approved"

DECISION_MERGE = "merge"
DECISION_SEPARATE = "separate"


def base_name(name: str) -> str:
    """The name with pharmacy tags removed, whitespace collapsed, uppercased."""
    s = _TAGS.sub(" ", str(name))
    s = _ZZ_PREFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip().upper()


def _words(name: str) -> list[str]:
    s = base_name(name)
    for pattern, repl in _UNIT_SPELLINGS:
        s = pattern.sub(repl, s)
    return [w.strip(".") for w in re.split(r"[^A-Z0-9.%]+", s) if w.strip(".")]


def same_spelling(a: str, b: str) -> bool:
    """Identical apart from spacing, punctuation, unit spelling and word
    order — every letter and every number the same. "PRONATE - 40 TAB" and
    "PRONATE-40TAB" are; "DOLO 200MG" and "DOLO 800MG" are not."""
    wa, wb = _words(a), _words(b)
    if "".join(wa) == "".join(wb) or sorted(wa) == sorted(wb):
        return True
    # The same letters and digits in the same order, before unit spellings
    # are touched — a moved space can stop a unit from being recognised
    # ("D -PROTIN 500GMV/F" vs "D -PROTIN 500GM V/F": "GM" runs into "V").
    return re.sub(r"[^A-Z0-9]", "", base_name(a)) == re.sub(r"[^A-Z0-9]", "", base_name(b))


def _numbers(name: str) -> list[float]:
    return sorted(float(x) for x in re.findall(r"\d+(?:\.\d+)?", base_name(name)))


def number_changed(a: str, b: str) -> bool:
    """Each name has a number the other doesn't — a strength, pack size or
    count was *replaced* ("20GM" -> "30GM"), not just added or dropped."""
    na, nb = Counter(_numbers(a)), Counter(_numbers(b))
    return bool(na - nb) and bool(nb - na)


def name_similarity(a: str, b: str) -> float:
    """Only for ordering review suggestions — never decides a merge."""
    return SequenceMatcher(None, base_name(a), base_name(b)).ratio()


def catalog_name_keys(name: str) -> list[str]:
    """How a stock-statement name can appear in the item list: as itself, or
    — the item list cuts product names to CATALOG_NAME_MAX characters — as
    its first 25. Uppercased and stripped, like the item list's names are
    when indexed. The one place this rule lives (CodeLookup and
    analysis.find_unmatched_skus both use it)."""
    key = str(name).strip().upper()
    return [key, key[:CATALOG_NAME_MAX].rstrip()] if len(key) > CATALOG_NAME_MAX else [key]


class CodeLookup:
    """Stock-statement name -> item code, from the item list(s).

    Built from the current item list plus every earlier name the rename log
    has seen for a code (db.item_name_changes, which grows with each monthly
    upload). Product names and rename-log names are the strong match; the
    item list's long name, and the 25-character cut-off, are weaker (see the
    module docstring). A name that maps to more than one code is treated as
    unknown rather than guessed.
    """

    def __init__(self, catalog: pd.DataFrame | None = None, rename_log: pd.DataFrame | None = None):
        self._strong: dict[str, set[str]] = defaultdict(set)
        self._long: dict[str, set[str]] = defaultdict(set)

        def _add(index, names, codes):
            for name, code in zip(names, codes, strict=True):
                if isinstance(name, str) and name.strip():
                    index[name.strip().upper()].add(str(code))

        if catalog is not None and not catalog.empty:
            _add(self._strong, catalog["product_name"], catalog["code"])
            _add(self._long, catalog["long_name"], catalog["code"])
        if rename_log is not None and not rename_log.empty:
            _add(self._strong, rename_log["old_name"], rename_log["code"])
            _add(self._strong, rename_log["new_name"], rename_log["code"])

    def __bool__(self) -> bool:
        return bool(self._strong or self._long)

    def code_for(self, name: str) -> tuple[str | None, bool]:
        """(code, exact). ``exact`` is False when the code was only found via
        the item list's long name or its 25-character cut-off."""
        keys = catalog_name_keys(name)
        for codes, exact in (
            (self._strong.get(keys[0]), True),
            (self._long.get(keys[0]), False),
            (self._strong.get(keys[1]) if len(keys) > 1 else None, False),
        ):
            if codes:
                return (next(iter(codes)), exact) if len(codes) == 1 else (None, False)
        return None, False


@dataclass
class Handoff:
    """One name vanishing while holding stock, and a new name in the same
    brand opening with exactly that stock, in the very next report."""

    old_name: str
    new_name: str
    brand: str | None
    period_end: pd.Timestamp  # end of the report the new name first appears in
    period_start: pd.Timestamp
    stock: float
    value: float  # the new name's closing value in that report
    same_spelling: bool
    similarity: float


@dataclass
class Resolution:
    """The outcome: what every source name is displayed as, plus what was
    merged (and why) and what's waiting for someone to decide."""

    display_name: dict[str, str]  # every source name -> its item's display name
    merges: list[dict] = field(default_factory=list)  # applied: old, new, reason
    suggestions: list[dict] = field(default_factory=list)  # pending review
    decided: list[dict] = field(default_factory=list)  # stored decisions + their effect
    aliases: dict[str, list[str]] = field(default_factory=dict)  # display -> other names

    def is_identity(self) -> bool:
        return not self.merges


class _Groups:
    """Union-find over names, refusing any merge the guards rule out."""

    def __init__(self, names, codes: dict[str, str | None], reports: dict[str, set]):
        self.parent = {n: n for n in names}
        self.codes = {n: ({codes[n]} if codes.get(n) else set()) for n in names}
        self.reports = {n: set(reports.get(n, ())) for n in names}
        self.members = {n: {n} for n in names}
        # name -> names a person said are a different item ("separate")
        self.kept_apart: dict[str, set[str]] = defaultdict(set)

    def keep_apart(self, a: str, b: str) -> None:
        self.kept_apart[a].add(b)
        self.kept_apart[b].add(a)

    def find(self, n: str) -> str:
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]
            n = self.parent[n]
        return n

    def why_not(self, a: str, b: str, *, allow_cooccurring: bool = False) -> str | None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        small, other = sorted((ra, rb), key=lambda r: len(self.members[r]))
        if any(self.kept_apart.get(n, set()) & self.members[other] for n in self.members[small]):
            return "marked as different items"
        if self.codes[ra] and self.codes[rb] and self.codes[ra] != self.codes[rb]:
            return "different item codes"
        if not allow_cooccurring and self.reports[ra] & self.reports[rb]:
            return "both names appear in the same report"
        return None

    def union(self, a: str, b: str, *, allow_cooccurring: bool = False) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb or self.why_not(a, b, allow_cooccurring=allow_cooccurring):
            return False
        self.parent[rb] = ra
        self.codes[ra] |= self.codes.pop(rb)
        self.reports[ra] |= self.reports.pop(rb)
        self.members[ra] |= self.members.pop(rb)
        return True


def find_handoffs(entries: pd.DataFrame) -> list[Handoff]:
    """Every same-brand vanished/new pair between consecutive reports where
    the stock carried over exactly, paired one-to-one: when several names
    could match (several items in one brand holding 2 units, say), the
    closest spelling wins and each name is used at most once per report.
    """
    reports = (
        entries[["report_id", "period_start", "period_end"]]
        .drop_duplicates("report_id")
        .sort_values(["period_end", "period_start"])
    )
    by_report = {rid: df for rid, df in entries.groupby("report_id")}
    out: list[Handoff] = []
    ids = list(reports["report_id"])
    for prev_id, cur_id in pairwise(ids):
        prev, cur = by_report[prev_id], by_report[cur_id]
        prev_names, cur_names = set(prev["sku"]), set(cur["sku"])
        vanished = prev[~prev["sku"].isin(cur_names) & (prev["closing_stock"] > 0)]
        new = cur[~cur["sku"].isin(prev_names) & (cur["opening_stock"] > 0)]
        if vanished.empty or new.empty:
            continue
        pairs = vanished[["sku", "brand", "closing_stock"]].merge(
            new[["sku", "brand", "opening_stock", "value", "period_end", "period_start"]],
            on="brand",
            suffixes=("_old", "_new"),
        )
        pairs = pairs[(pairs["opening_stock"] - pairs["closing_stock"]).abs() < 0.5]
        if pairs.empty:
            continue
        candidates = []
        for r in pairs.itertuples(index=False):
            spelled = same_spelling(r.sku_old, r.sku_new)
            sim = name_similarity(r.sku_old, r.sku_new)
            candidates.append((not spelled, -sim, r, spelled, sim))
        candidates.sort(key=lambda c: (c[0], c[1], c[2].sku_old, c[2].sku_new))
        used_old: set[str] = set()
        used_new: set[str] = set()
        for _, _, r, spelled, sim in candidates:
            if r.sku_old in used_old or r.sku_new in used_new:
                continue
            used_old.add(r.sku_old)
            used_new.add(r.sku_new)
            out.append(
                Handoff(
                    old_name=r.sku_old,
                    new_name=r.sku_new,
                    brand=r.brand,
                    period_end=r.period_end,
                    period_start=r.period_start,
                    stock=float(r.closing_stock),
                    value=float(r.value),
                    same_spelling=spelled,
                    similarity=round(sim, 3),
                )
            )
    return out


def resolve(
    entries: pd.DataFrame,
    codes: CodeLookup | None = None,
    decisions: pd.DataFrame | None = None,
) -> Resolution:
    """Group every name in ``entries`` into items (see the module docstring).

    ``decisions`` is db.get_merge_decisions(): one row per decision, with
    ``decision`` either "merge" or "separate".
    """
    if entries.empty:
        return Resolution(display_name={})
    codes = codes or CodeLookup()
    names = sorted(set(entries["sku"]))
    looked_up = {n: codes.code_for(n) for n in names} if codes else {}
    code_of = {n: c for n, (c, _) in looked_up.items()}
    reports_of = entries.groupby("sku")["report_id"].agg(set).to_dict()
    groups = _Groups(names, code_of, reports_of)
    merges: list[dict] = []

    def _merge(a: str, b: str, reason: str, **kw) -> bool:
        if groups.union(a, b, **kw):
            merges.append({"old_name": a, "new_name": b, "reason": reason})
            return True
        return False

    rows = list(decisions.itertuples(index=False)) if decisions is not None else []
    known = groups.parent

    # 0. "Separate" first — it overrides every rule below, codes included.
    for d in rows:
        if d.decision == DECISION_SEPARATE and d.old_name in known and d.new_name in known:
            groups.keep_apart(d.old_name, d.new_name)

    # 1. Item codes. The POS is the authority here, so the same-report guard
    # doesn't apply (rows are summed if it ever happens). Names matched
    # exactly are merged outright; one matched only on its first 25
    # characters or via a long name joins only a name spelled the same.
    by_code: dict[str, list[str]] = defaultdict(list)
    for n, c in code_of.items():
        if c:
            by_code[c].append(n)
    for members in by_code.values():
        exact = [m for m in members if looked_up[m][1]]
        for other in exact[1:]:
            _merge(exact[0], other, REASON_CODE, allow_cooccurring=True)
        placed = list(exact)
        for m in (m for m in members if not looked_up[m][1]):
            match = next((p for p in placed if same_spelling(p, m)), None)
            if match is not None:
                _merge(match, m, REASON_CODE, allow_cooccurring=True)
            placed.append(m)

    # 2. Approved merges — a person looked at these.
    decided: list[dict] = []
    for d in rows:
        entry = {
            "old_name": d.old_name,
            "new_name": d.new_name,
            "decision": d.decision,
            "decided_at": str(d.decided_at),
        }
        if d.old_name not in known or d.new_name not in known:
            entry["effect"] = "names no longer in the imported reports"
        elif d.decision == DECISION_SEPARATE:
            entry["effect"] = "kept separate"
        else:
            blocked = groups.why_not(d.old_name, d.new_name)
            if blocked and groups.find(d.old_name) != groups.find(d.new_name):
                entry["effect"] = f"not merged: {blocked}"
            else:
                _merge(d.old_name, d.new_name, REASON_APPROVED)
                entry["effect"] = "merged"
        decided.append(entry)

    # 3. Pharmacy tags: same name once (NON)/(RECALL)/ZZ are removed.
    by_base: dict[str, list[str]] = defaultdict(list)
    for n in names:
        by_base[base_name(n)].append(n)
    for members in by_base.values():
        for other in members[1:]:
            _merge(members[0], other, REASON_TAG)

    # 4 + suggestions. Stock carried over between consecutive reports.
    suggestions: list[dict] = []
    for h in find_handoffs(entries):
        if groups.find(h.old_name) == groups.find(h.new_name):
            continue
        if h.same_spelling and _merge(h.old_name, h.new_name, REASON_SPELLING):
            continue
        if groups.why_not(h.old_name, h.new_name):
            continue  # can't merge even if approved (or already rejected) — don't ask
        if number_changed(h.old_name, h.new_name):
            continue  # a different strength/pack size is a different product — don't ask
        suggestions.append(
            {
                "old_name": h.old_name,
                "new_name": h.new_name,
                "brand": h.brand,
                "period_start": h.period_start.date().isoformat(),
                "period_end": h.period_end.date().isoformat(),
                "stock": h.stock,
                "value": round(h.value, 2),
                "similarity": h.similarity,
            }
        )

    # Display name: what the item is called in the most recent report it's in.
    latest = (
        entries[["sku", "period_end", "period_start"]]
        .sort_values(["period_end", "period_start"])
        .drop_duplicates("sku", keep="last")
        .set_index("sku")
    )
    members_of: dict[str, list[str]] = defaultdict(list)
    for n in names:
        members_of[groups.find(n)].append(n)
    display: dict[str, str] = {}
    aliases: dict[str, list[str]] = {}
    for members in members_of.values():
        if len(members) == 1:
            display[members[0]] = members[0]
            continue
        newest = max(
            members,
            key=lambda m: (latest.at[m, "period_end"], latest.at[m, "period_start"], m),
        )
        for m in members:
            display[m] = newest
        aliases[newest] = sorted(m for m in members if m != newest)

    # A merge listed against the names people will actually see.
    for m in merges:
        m["display_name"] = display[m["new_name"]]
    suggestions.sort(key=lambda s: (-s["value"], s["old_name"]))
    return Resolution(
        display_name=display,
        merges=merges,
        suggestions=suggestions,
        decided=decided,
        aliases=aliases,
    )


_NUMERIC_COLS = [
    "opening_stock",
    "purchase",
    "purchase_free",
    "other_receipt",
    "sales",
    "sales_free",
    "other_issue",
    "closing_stock",
    "value",
]


def apply(entries: pd.DataFrame, resolution: Resolution) -> pd.DataFrame:
    """``entries`` with ``sku`` rewritten to each item's display name, and the
    original name kept in ``source_sku``. Every analysis keys on ``sku``, so
    this is all it takes for a renamed item's whole history to count as one.

    Two names merged by item code could in principle both appear in one
    report; their rows are summed into one, the same way the parser
    combines a SKU listed twice in one file.
    """
    out = entries.copy()
    out["source_sku"] = out["sku"]
    if resolution.is_identity():
        return out
    out["sku"] = out["sku"].map(resolution.display_name).fillna(out["sku"])
    if out.duplicated(["report_id", "sku"]).any():
        keys = ["report_id", "sku"]
        first_cols = [c for c in out.columns if c not in keys and c not in _NUMERIC_COLS]
        out = out.groupby(keys, as_index=False, sort=False).agg(
            {**{c: "first" for c in first_cols}, **{c: "sum" for c in _NUMERIC_COLS}}
        )[list(entries.columns) + ["source_sku"]]
    return out
