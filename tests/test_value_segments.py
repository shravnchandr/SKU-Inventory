"""Unit tests for the ABC/value segmentation feature.

_compute_value_tiers: cumulative-value Pareto tier (A/B/C) per SKU, ranked
by value on hand. _compute_value_segments/_value_segment_summary: cross
that with movement (reusing the existing status classification), producing
the count/value aggregate shown as tiles on the Dashboard.
"""

import pandas as pd
import pytest

from src.gowri_proj.analysis import (
    SEGMENT_ORDER,
    _compute_value_segments,
    _compute_value_tiers,
    _value_segment_summary,
    summarize_history,
)


def _current(rows: list[dict]) -> pd.DataFrame:
    """rows: dicts with sku, value, closing_stock (defaults to 1 if omitted)."""
    df = pd.DataFrame(rows)
    if "closing_stock" not in df.columns:
        df["closing_stock"] = 1.0
    return df


def test_tier_boundaries_split_a_b_c():
    # 700/200/100 out of 1000 total: A ends where cumulative-before first
    # reaches 70% (just SKU 1), B continues to 90% (SKU 2 lands there since
    # cumulative-before it is exactly 70%, not yet past), C is the rest.
    current = _current(
        [
            {"sku": "S1", "value": 700},
            {"sku": "S2", "value": 200},
            {"sku": "S3", "value": 100},
        ]
    )
    tiers = _compute_value_tiers(current, a_pct=70, b_pct=90)
    assert tiers.to_dict() == {"S1": "A", "S2": "B", "S3": "C"}


def test_a_single_dominant_sku_lands_in_tier_a_not_c():
    # A SKU that alone makes up more than a_pct of total value must still
    # be tier A — it's judged by the cumulative *before* it (0%, since
    # nothing else outranks it), not by the fact that including it pushes
    # cumulative past the cutoff. Getting this backwards would put the
    # single highest-value SKU in the long tail, which defeats the point.
    current = _current(
        [
            {"sku": "DOMINANT", "value": 10000},
            {"sku": "TINY", "value": 50},
        ]
    )
    tiers = _compute_value_tiers(current, a_pct=70, b_pct=90)
    assert tiers["DOMINANT"] == "A"
    assert tiers["TINY"] == "C"


def test_custom_cutoffs_are_respected():
    current = _current(
        [{"sku": "S1", "value": 10}, {"sku": "S2", "value": 10}, {"sku": "S3", "value": 10}]
    )
    tiers = _compute_value_tiers(current, a_pct=40, b_pct=70)
    # Cumulative-before: S1=0% (<40 -> A), S2=33.3% (<40 -> A), S3=66.7% (<70 -> B)
    assert tiers.to_dict() == {"S1": "A", "S2": "A", "S3": "B"}


def test_all_zero_value_does_not_divide_by_zero():
    current = _current([{"sku": "S1", "value": 0}, {"sku": "S2", "value": 0}])
    tiers = _compute_value_tiers(current, a_pct=70, b_pct=90)
    assert tiers.to_dict() == {"S1": "C", "S2": "C"}


def test_out_of_stock_skus_are_excluded_from_tiering():
    # closing_stock <= 0 has nothing to tie up capital in right now — no
    # value tier to review.
    current = _current(
        [
            {"sku": "STOCKED", "value": 100, "closing_stock": 5},
            {"sku": "GONE", "value": 999, "closing_stock": 0},
        ]
    )
    tiers = _compute_value_tiers(current, a_pct=70, b_pct=90)
    assert list(tiers.index) == ["STOCKED"]


def _merged(rows: list[dict]) -> pd.DataFrame:
    """rows: dicts with sku, brand, value, status, days_of_cover, closing_stock.
    closing_stock/days_of_cover default per-row, not just per-column — a
    plain pd.DataFrame(rows) leaves a NaN wherever one row happens to omit a
    key some other row provided, which silently drops that row out of
    "currently stocked" (NaN > 0 is False).
    """
    df = pd.DataFrame(rows)
    if "closing_stock" not in df.columns:
        df["closing_stock"] = 1.0
    else:
        df["closing_stock"] = df["closing_stock"].fillna(1.0)
    if "days_of_cover" not in df.columns:
        df["days_of_cover"] = 30.0
    else:
        df["days_of_cover"] = df["days_of_cover"].fillna(30.0)
    return df


@pytest.mark.parametrize(
    "status,expected_movement",
    [
        ("dead_stock", "non_moving"),
        ("overstock", "slow"),
        ("low_stock", "fast"),
        ("healthy", "fast"),
    ],
)
def test_movement_mapping(status, expected_movement):
    merged = _merged([{"sku": "S1", "brand": "B", "value": 100, "status": status}])
    segments = _compute_value_segments(merged, a_pct=70, b_pct=90)
    assert segments.iloc[0]["movement"] == expected_movement


@pytest.mark.parametrize("status", ["out_of_stock", "returned"])
def test_out_of_stock_and_returned_never_appear_in_any_segment(status):
    merged = _merged(
        [
            {"sku": "EXCLUDED", "brand": "B", "value": 999, "status": status, "closing_stock": 0},
            {"sku": "INCLUDED", "brand": "B", "value": 100, "status": "healthy"},
        ]
    )
    segments = _compute_value_segments(merged, a_pct=70, b_pct=90)
    assert list(segments["sku"]) == ["INCLUDED"]


def test_compute_value_segments_handles_nothing_stocked_without_crashing():
    # Everything out of stock/returned — no SKU left to build a "tier-
    # movement" segment string out of. Must return an empty frame with the
    # right columns, not blow up on an empty-array string concatenation.
    merged = _merged(
        [{"sku": "GONE", "brand": "B", "value": 999, "status": "out_of_stock", "closing_stock": 0}]
    )
    segments = _compute_value_segments(merged, a_pct=70, b_pct=90)
    assert segments.empty
    assert list(segments.columns) == [
        "brand",
        "sku",
        "value",
        "days_of_cover",
        "status",
        "tier",
        "movement",
        "segment",
    ]


def test_value_segment_summary_always_returns_all_nine_in_order():
    merged = _merged([{"sku": "S1", "brand": "B", "value": 100, "status": "dead_stock"}])
    segments = _compute_value_segments(merged, a_pct=70, b_pct=90)
    summary = _value_segment_summary(segments)
    assert [(s["tier"], s["movement"]) for s in summary] == SEGMENT_ORDER
    a_non_moving = next(s for s in summary if s["tier"] == "A" and s["movement"] == "non_moving")
    assert a_non_moving["count"] == 1
    assert a_non_moving["value"] == 100.0
    empty_segment = next(s for s in summary if s["tier"] == "C" and s["movement"] == "fast")
    assert empty_segment == {"tier": "C", "movement": "fast", "count": 0, "value": 0.0}


def _row(
    report_id,
    period_start,
    period_end,
    brand,
    sku,
    *,
    sales=0.0,
    purchase=0.0,
    closing_stock=1.0,
    value=100.0,
):
    return {
        "report_id": report_id,
        "period_start": pd.Timestamp(period_start),
        "period_end": pd.Timestamp(period_end),
        "period_days": (pd.Timestamp(period_end) - pd.Timestamp(period_start)).days + 1,
        "company": "TEST PHARMACY",
        "location": "TEST CITY",
        "brand": brand,
        "sku": sku,
        "opening_stock": closing_stock,
        "purchase": purchase,
        "purchase_free": 0.0,
        "other_receipt": 0.0,
        "sales": sales,
        "sales_free": 0.0,
        "other_issue": 0.0,
        "closing_stock": closing_stock,
        "value": value,
    }


def test_end_to_end_through_summarize_history_places_high_value_dead_sku_in_a_non_moving():
    entries = pd.DataFrame(
        [
            # High-value, hasn't sold in the trailing window -> dead_stock -> A/non_moving.
            _row(
                1,
                "2026-05-01",
                "2026-07-31",
                "BRAND",
                "HIGH VALUE DEAD SKU",
                closing_stock=10,
                value=90000.0,
            ),
            # Low value, selling steadily -> healthy -> C/fast.
            _row(
                1,
                "2026-05-01",
                "2026-07-31",
                "BRAND",
                "LOW VALUE FAST SKU",
                sales=30,
                closing_stock=10,
                value=100.0,
            ),
        ]
    )
    summary = summarize_history(entries, dead_stock_days=90)
    by_segment = {(s["tier"], s["movement"]): s for s in summary.value_segments}
    assert by_segment[("A", "non_moving")]["count"] == 1
    assert by_segment[("A", "non_moving")]["value"] == 90000.0
    assert by_segment[("C", "fast")]["count"] == 1
    skus_by_name = {r["sku"]: r for _, r in summary.value_segment_skus.iterrows()}
    assert skus_by_name["HIGH VALUE DEAD SKU"]["segment"] == "A-non_moving"
    assert skus_by_name["LOW VALUE FAST SKU"]["segment"] == "C-fast"
