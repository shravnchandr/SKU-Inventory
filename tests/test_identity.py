"""identity.py: deciding which stock-statement names are the same item.

The rules (see the module docstring) err hard on the side of *not* merging:
for medicines a different number or word is a different product ("DOLO
200MG" vs "DOLO 800MG", "GLOEYE TAB" vs "GLOEYE PLUS TAB"), so anything
short of an item code, a pharmacy tag or an identical spelling with the
stock carried over has to be approved by a person — and a person's
"separate" overrides everything, codes included.
"""

import pandas as pd
import pytest

from src.gowri_proj import identity
from src.gowri_proj.analysis import summarize_history
from src.gowri_proj.identity import CodeLookup, resolve, same_spelling

NUMERIC = ["opening_stock", "purchase", "purchase_free", "other_receipt", "sales",
           "sales_free", "other_issue", "closing_stock", "value"]  # fmt: skip

MONTHS = [("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-28"), ("2026-03-01", "2026-03-31"),
          ("2026-04-01", "2026-04-30"), ("2026-05-01", "2026-05-31")]  # fmt: skip


def _row(
    sku,
    opening=0.0,
    closing=0.0,
    sales=0.0,
    brand="BRAND",
    purchase=0.0,
    value=None,
    other_issue=0.0,
):
    return {
        "brand": brand, "sku": sku, "opening_stock": opening, "purchase": purchase,
        "purchase_free": 0.0, "other_receipt": 0.0, "sales": sales, "sales_free": 0.0,
        "other_issue": other_issue, "closing_stock": closing,
        "value": closing * 10.0 if value is None else value,
    }  # fmt: skip


def _entries(*months_rows):
    """One report per argument (a list of _row dicts), on consecutive months."""
    frames = []
    for i, rows in enumerate(months_rows):
        start, end = MONTHS[i]
        df = pd.DataFrame(rows)
        df["report_id"] = i + 1
        df["period_start"] = pd.Timestamp(start)
        df["period_end"] = pd.Timestamp(end)
        df["period_days"] = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
        df["company"] = "T"
        df["location"] = "T"
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _catalog(*pairs):
    return pd.DataFrame([{"code": c, "product_name": n, "long_name": None} for c, n in pairs])


def _same(res, a, b):
    return res.display_name[a] == res.display_name[b]


# ---------- the spelling rule on its own ----------


@pytest.mark.parametrize(
    "a, b",
    [
        ("DOLO 650", "DOLO-650"),
        ("IODEX 8G", "IODEX 8GM"),
        ("PRONATE - 40 TAB", "PRONATE-40TAB"),
        ("ROSLOY GOLD 10 MG CAPS", "ROSLOY GOLD 10MG CAPS"),
        ("BIO PAPAYA F/W 50ML", "BIO F/W PAPAYA 50ML"),
        ("SHELCAL 500 TAB  BOTTLE 30S", "SHELCAL 500 TAB 30'S[BOTTLE]"),
        ("AMLOKIND 5MG TAB", "AMLOKIND 5MG TAB (NON)"),
    ],
)
def test_same_spelling(a, b):
    assert same_spelling(a, b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("DOLO 200MG", "DOLO 800MG"),  # strength
        ("CLINDAC A GEL 20GM", "CLINDAC A GEL 30GM"),  # pack size
        ("GLOEYE TAB", "GLOEYE PLUS TAB"),  # variant word added
        ("TOPLAP CREAM 10G", "TOPLAP GEL 10GM"),  # dosage form
        ("ROSUMAC GOLD CAP", "ROSUMAC GOLD 10MG CAP"),  # number added
        ("H K BIOTIN TAB 40'S", "H K BIOTIN TAB 60'S"),  # count
        ("MONOCEF-SB 1GM INJ", "MONOCEF-SB 1.5GM INJ"),
    ],
)
def test_not_same_spelling(a, b):
    assert not same_spelling(a, b)


# ---------- pharmacy tags ----------


def test_non_tag_merges_even_across_months_with_no_stock_in_between():
    # Tagged (NON) and returned, gone for a month, then sold again untagged.
    e = _entries(
        [_row("EPIVAL TAB", opening=100, closing=80, sales=20)],
        [_row("EPIVAL TAB (NON)", opening=80, closing=0, other_issue=80)],
        [],
        [_row("EPIVAL TAB", opening=0, purchase=30, closing=25, sales=5)],
    )
    res = resolve(e)
    assert _same(res, "EPIVAL TAB", "EPIVAL TAB (NON)")
    assert res.display_name["EPIVAL TAB (NON)"] == "EPIVAL TAB"  # latest report's name
    assert res.aliases["EPIVAL TAB"] == ["EPIVAL TAB (NON)"]


@pytest.mark.parametrize(
    "tagged", ["ZZNICOTEX-2 PAN", "NICOTEX-2 PAN (RECALL)", "NICOTEX-2 PAN NON)"]
)
def test_other_tags(tagged):
    e = _entries([_row("NICOTEX-2 PAN", 5, 5)], [_row(tagged, 5, 5)])
    assert _same(resolve(e), "NICOTEX-2 PAN", tagged)


def test_tag_merge_blocked_when_item_list_says_different_codes():
    e = _entries([_row("NIV CREAM", 5, 5)], [_row("ZZNIV CREAM", 5, 5)])
    codes = CodeLookup(_catalog(("A1", "NIV CREAM"), ("B2", "ZZNIV CREAM")))
    assert not _same(resolve(e, codes), "NIV CREAM", "ZZNIV CREAM")


def test_tag_merge_blocked_when_both_names_are_in_the_same_report():
    # One report never lists an item twice, so these are two items.
    e = _entries([_row("X TAB", 5, 5), _row("X TAB (NON)", 3, 3)])
    assert not _same(resolve(e), "X TAB", "X TAB (NON)")


# ---------- stock carried over ----------


def test_same_spelling_with_stock_carried_over_merges():
    e = _entries([_row("PRONATE - 40 TAB", 70, 60, 10)], [_row("PRONATE-40TAB", 60, 50, 10)])
    res = resolve(e)
    assert _same(res, "PRONATE - 40 TAB", "PRONATE-40TAB")
    assert res.merges[0]["reason"] == identity.REASON_SPELLING
    assert res.suggestions == []


def test_same_spelling_without_stock_carried_over_does_not_merge():
    e = _entries([_row("PRONATE - 40 TAB", 70, 60, 10)], [_row("PRONATE-40TAB", 12, 2, 10)])
    res = resolve(e)
    assert not _same(res, "PRONATE - 40 TAB", "PRONATE-40TAB")
    assert res.suggestions == []


def test_dolo_200_to_800_is_never_merged_nor_even_suggested():
    # Every condition except the name lines up: same brand, Dolo 200 vanished
    # holding 15, Dolo 800 appeared opening with exactly 15. A different
    # strength is a different product — not something to ask about.
    e = _entries([_row("DOLO 200MG TAB", 20, 15, 5)], [_row("DOLO 800MG TAB", 15, 10, 5)])
    res = resolve(e)
    assert not _same(res, "DOLO 200MG TAB", "DOLO 800MG TAB")
    assert res.suggestions == []


@pytest.mark.parametrize(
    "old, new",
    [
        # The pharmacy's own examples of wrong suggestions: pack size changed.
        ("CLINDAC A GEL 20GM", "CLINDAC A GEL 30GM"),
        ("DR.ORTHO OIL 100ML", "DR.ORTHO OIL 120ML"),
        ("SEBA LIQUID FACE & BODYWASH 400ML", "SEBA LIQUID FACE & BODYWASH 200ML"),
        ("NEOSHIELD ULTRA GEL 60GM", "NEOSHIELD ULTRA GEL 50GM"),
        ("PAMP PREM L 48'S", "PAMP PREM L 44S"),
    ],
)
def test_changed_pack_size_is_not_suggested(old, new):
    e = _entries([_row(old, 20, 15, 5)], [_row(new, 15, 10, 5)])
    res = resolve(e)
    assert not _same(res, old, new)
    assert res.suggestions == []


@pytest.mark.parametrize(
    "old, new",
    [
        ("BIGEN BEARD BROWN BLACK B102", "BIGEN BEARD BROWN BLACK B102 40GM"),  # pack size added
        ("D3 MUST 60K TAB (8'S)", "D3 MUST 60K TAB"),  # count dropped
        ("GLOEYE TAB", "GLOEYE PLUS TAB"),  # same numbers, a word added
    ],
)
def test_number_only_added_or_removed_or_word_differs_is_still_suggested(old, new):
    e = _entries([_row(old, 20, 15, 5)], [_row(new, 15, 10, 5)])
    res = resolve(e)
    assert not _same(res, old, new)
    assert [(s["old_name"], s["new_name"]) for s in res.suggestions] == [(old, new)]


def test_moved_space_that_hides_a_unit_is_still_same_spelling():
    assert same_spelling("D -PROTIN 500GMV/F", "D -PROTIN 500GM V/F")
    e = _entries([_row("D -PROTIN 500GMV/F", 5, 3, 2)], [_row("D -PROTIN 500GM V/F", 3, 3)])
    assert _same(resolve(e), "D -PROTIN 500GMV/F", "D -PROTIN 500GM V/F")


def test_different_brand_is_not_even_suggested():
    e = _entries(
        [_row("GLOEYE TAB", 20, 15, 5, brand="A")], [_row("GLOEYE PLUS TAB", 15, 10, 5, brand="B")]
    )
    res = resolve(e)
    assert res.suggestions == [] and not res.merges


def test_several_candidates_pair_one_to_one_by_closest_spelling():
    # Two items in one brand both holding 2 units vanish; two new names
    # open with 2 each. Each pairs with its own respelling, not crosswise.
    e = _entries(
        [_row("MOOV SPRAY 35G", 3, 2, 1), _row("IODEX 8G", 3, 2, 1)],
        [_row("IODEX 8GM", 2, 2), _row("MOOV SPRAY 35GM", 2, 2)],
    )
    res = resolve(e)
    assert _same(res, "IODEX 8G", "IODEX 8GM")
    assert _same(res, "MOOV SPRAY 35G", "MOOV SPRAY 35GM")
    assert not _same(res, "IODEX 8G", "MOOV SPRAY 35GM")


# ---------- item codes ----------


def test_same_code_merges_even_when_names_differ_beyond_spelling():
    # The POS is the authority: it renamed this code.
    e = _entries([_row("OTRIVIN A DROPS 10ML", 5, 5)], [_row("OTRIVIN ADULT OXY FAST 10ML", 5, 5)])
    log = pd.DataFrame(
        [
            {
                "code": "OT1",
                "old_name": "OTRIVIN A DROPS 10ML",
                "new_name": "OTRIVIN ADULT OXY FAST 10ML",
            }
        ]
    )
    res = resolve(e, CodeLookup(_catalog(("OT1", "OTRIVIN ADULT OXY FAST 10ML")), log))
    assert _same(res, "OTRIVIN A DROPS 10ML", "OTRIVIN ADULT OXY FAST 10ML")
    assert res.merges[0]["reason"] == identity.REASON_CODE


def test_25_char_prefix_code_needs_matching_numbers():
    # The item list cut both strengths' names to the same 25 characters.
    # A shared prefix alone mustn't merge a 200MG and a 400MG.
    long200 = "PARACETAMOL SUSPENSION 60ML 200MG"
    long400 = "PARACETAMOL SUSPENSION 60ML 400MG"
    prefix = long200[:25]
    assert long400[:25] == prefix
    e = _entries([_row(long200, 5, 5)], [_row(long400, 5, 5)])
    res = resolve(e, CodeLookup(_catalog(("P1", prefix))))
    assert not _same(res, long200, long400)


def test_25_char_prefix_code_merges_only_with_the_same_spelling():
    full = "SIMILAC PLUS [IQ] NO-1 400GM"
    respelled = "SIMILAC PLUS [IQ] NO-1 400 GM"
    cat = pd.DataFrame([{"code": "SIM30", "product_name": full[:25], "long_name": None}])
    e = _entries([_row(full, 5, 5)], [_row(respelled, 5, 5)])
    assert _same(resolve(e, CodeLookup(cat)), full, respelled)


def test_long_name_code_alone_does_not_merge_different_variants():
    # The item list has codes whose short and long names name different
    # variants (seen in real data). Without the same spelling, that's not
    # sure enough to merge automatically — it's asked about instead.
    cat = pd.DataFrame(
        [
            {
                "code": "PA1",
                "product_name": "P/A S/C COOL BLUE 84GM",
                "long_name": "P/A S/C CLASSIC 84GM",
            }
        ]
    )
    e = _entries([_row("P/A S/C CLASSIC 84GM", 5, 5)], [_row("P/A S/C COOL BLUE 84GM", 5, 5)])
    res = resolve(e, CodeLookup(cat))
    assert not _same(res, "P/A S/C CLASSIC 84GM", "P/A S/C COOL BLUE 84GM")
    assert [(s["old_name"], s["new_name"]) for s in res.suggestions] == [
        ("P/A S/C CLASSIC 84GM", "P/A S/C COOL BLUE 84GM")
    ]


def test_ambiguous_name_is_treated_as_unknown():
    codes = CodeLookup(_catalog(("A", "DOLO 650"), ("B", "DOLO 650")))
    assert codes.code_for("DOLO 650") == (None, False)


# ---------- people's decisions ----------


def _decisions(*rows):
    return pd.DataFrame(
        [
            {"old_name": o, "new_name": n, "decision": d, "decided_at": "2026-10-01 10:00:00"}
            for o, n, d in rows
        ]
    )


def _dolo():
    return _entries([_row("DOLO 200MG TAB", 20, 15, 5)], [_row("DOLO 800MG TAB", 15, 10, 5)])


def test_approving_a_suggestion_merges_it():
    res = resolve(_dolo(), decisions=_decisions(("DOLO 200MG TAB", "DOLO 800MG TAB", "merge")))
    assert _same(res, "DOLO 200MG TAB", "DOLO 800MG TAB")
    assert res.suggestions == []
    assert res.decided[0]["effect"] == "merged"
    assert res.merges[0]["reason"] == identity.REASON_APPROVED


def test_rejecting_a_suggestion_stops_it_being_suggested():
    res = resolve(_dolo(), decisions=_decisions(("DOLO 200MG TAB", "DOLO 800MG TAB", "separate")))
    assert not _same(res, "DOLO 200MG TAB", "DOLO 800MG TAB")
    assert res.suggestions == []
    assert res.decided[0]["effect"] == "kept separate"


def test_separate_undoes_an_automatic_merge_and_holds_through_a_third_name():
    # X -> X (NON) -> ZZX are all one item by tags. Separating X from ZZX
    # must keep them apart even though X (NON) links to both.
    e = _entries([_row("X TAB", 5, 5)], [_row("X TAB (NON)", 5, 5)], [_row("ZZX TAB", 5, 5)])
    assert _same(resolve(e), "X TAB", "ZZX TAB")
    res = resolve(e, decisions=_decisions(("X TAB", "ZZX TAB", "separate")))
    assert not _same(res, "X TAB", "ZZX TAB")


def test_separate_overrides_an_item_code():
    e = _entries([_row("CLINDAC A GEL 20GM", 5, 5)], [_row("CLINDAC A GEL 30GM", 5, 5)])
    log = pd.DataFrame(
        [{"code": "C1", "old_name": "CLINDAC A GEL 20GM", "new_name": "CLINDAC A GEL 30GM"}]
    )
    codes = CodeLookup(_catalog(("C1", "CLINDAC A GEL 30GM")), log)
    assert _same(resolve(e, codes), "CLINDAC A GEL 20GM", "CLINDAC A GEL 30GM")
    res = resolve(e, codes, _decisions(("CLINDAC A GEL 20GM", "CLINDAC A GEL 30GM", "separate")))
    assert not _same(res, "CLINDAC A GEL 20GM", "CLINDAC A GEL 30GM")


def test_approved_merge_cannot_join_items_with_different_codes():
    codes = CodeLookup(_catalog(("D2", "DOLO 200MG TAB"), ("D8", "DOLO 800MG TAB")))
    res = resolve(_dolo(), codes, _decisions(("DOLO 200MG TAB", "DOLO 800MG TAB", "merge")))
    assert not _same(res, "DOLO 200MG TAB", "DOLO 800MG TAB")
    assert res.decided[0]["effect"] == "not merged: different item codes"


def test_decision_for_names_no_longer_imported_is_reported_not_applied():
    res = resolve(_dolo(), decisions=_decisions(("GONE A", "GONE B", "merge")))
    assert res.decided[0]["effect"] == "names no longer in the imported reports"


# ---------- applying it ----------


def test_apply_keeps_totals_and_original_names():
    e = _entries(
        [_row("EPIVAL TAB", 100, 80, 20)], [_row("EPIVAL TAB (NON)", 80, 0, other_issue=80)]
    )
    out = identity.apply(e, resolve(e))
    assert set(out["sku"]) == {"EPIVAL TAB (NON)"}
    assert list(out["source_sku"]) == ["EPIVAL TAB", "EPIVAL TAB (NON)"]
    assert (
        out.groupby("report_id")["value"].sum().tolist()
        == e.groupby("report_id")["value"].sum().tolist()
    )


def test_apply_with_nothing_merged_changes_nothing_but_adds_source_sku():
    e = _entries([_row("A", 5, 5)], [_row("B", 5, 5)])
    out = identity.apply(e, resolve(e))
    pd.testing.assert_frame_equal(out.drop(columns="source_sku"), e)


def test_apply_sums_two_code_merged_names_in_one_report():
    # Two names the rename log ties to one code (a strong match), both in
    # one report: summed into one row.
    e = _entries([_row("SYP A 60ML", 5, 5), _row("SYP A LONGNAME 60ML", 2, 2)])
    log = pd.DataFrame(
        [{"code": "S1", "old_name": "SYP A LONGNAME 60ML", "new_name": "SYP A 60ML"}]
    )
    cat = pd.DataFrame([{"code": "S1", "product_name": "SYP A 60ML", "long_name": None}])
    out = identity.apply(e, resolve(e, CodeLookup(cat, log)))
    assert len(out) == 1
    assert out["closing_stock"].iloc[0] == 7


def test_renamed_item_keeps_its_sales_history_so_it_is_not_dead_stock():
    # Sold steadily for 4 months as "X", then tagged "(NON)" with stock left.
    # Keyed on raw names, "X TAB (NON)" has no sales history at all and is
    # flagged dead the moment the 90-day clock allows; joined up, its last
    # sale was last month.
    months = [[_row("X TAB", 100 - 10 * i, 90 - 10 * i, 10, purchase=0)] for i in range(4)]
    e = _entries(*months, [_row("X TAB (NON)", 60, 60, 0)])
    raw = summarize_history(e).enriched.set_index("sku")
    joined = summarize_history(identity.apply(e, resolve(e))).enriched.set_index("sku")
    assert raw.loc["X TAB (NON)", "status"] == "dead_stock"
    assert joined.loc["X TAB (NON)", "status"] != "dead_stock"
    assert joined.loc["X TAB (NON)", "sales"] > 0


def test_tagged_and_sent_back_stays_returned_not_out_of_stock():
    # Sold for months as "X", then tagged "(NON)" and the rest sent back.
    # Joined up, its earlier sales are in the trailing window — but "nothing
    # sold" is judged since it got its current name, so this is still a
    # deliberate return, not "out of stock, restock?".
    months = [[_row("X TAB", 100 - 10 * i, 90 - 10 * i, 10)] for i in range(3)]
    e = _entries(*months, [_row("X TAB (NON)", 70, 0, 0, other_issue=70)])
    raw = summarize_history(e).enriched.set_index("sku")
    joined = summarize_history(identity.apply(e, resolve(e))).enriched.set_index("sku")
    assert raw.loc["X TAB (NON)", "status"] == "returned"
    assert joined.loc["X TAB (NON)", "status"] == "returned"


def test_untagged_item_sold_out_is_still_out_of_stock():
    # The same rule must not touch an item that simply sold through.
    e = _entries([_row("Y TAB", 20, 10, 10)], [_row("Y TAB", 10, 0, 10)])
    joined = summarize_history(identity.apply(e, resolve(e))).enriched.set_index("sku")
    assert joined.loc["Y TAB", "status"] == "out_of_stock"
