"""Unit tests for analysis._status — pure function, priority order matters.

Each branch is checked in isolation, plus the priority order between
branches (e.g. an out-of-stock SKU that's also technically "dead" must
still classify as out_of_stock, not dead_stock — zero stock is the more
urgent fact).
"""

from src.gowri_proj.analysis import _status


def test_zero_stock_is_out_of_stock_regardless_of_everything_else():
    assert _status(closing_stock=0, is_dead=True, days_of_cover=500) == "out_of_stock"
    assert _status(closing_stock=0, is_dead=False, days_of_cover=0) == "out_of_stock"


def test_negative_stock_is_also_out_of_stock():
    # Negative closing stock happens with return/adjustment noise in the
    # source export — still "nothing to sell today" from a purchasing view.
    assert _status(closing_stock=-1, is_dead=False, days_of_cover=10) == "out_of_stock"


def test_zero_stock_with_a_return_and_nothing_sold_is_returned_not_out_of_stock():
    # Stock left via a return/write-off (is_returned=True), not a sale —
    # a deliberate "don't reorder this" signal, distinct from "sold out."
    assert (
        _status(closing_stock=0, is_dead=False, days_of_cover=float("inf"), is_returned=True)
        == "returned"
    )


def test_zero_stock_without_a_return_is_still_out_of_stock():
    # is_returned defaults to False — existing callers/behavior unaffected.
    assert _status(closing_stock=0, is_dead=False, days_of_cover=5) == "out_of_stock"
    assert (
        _status(closing_stock=0, is_dead=False, days_of_cover=5, is_returned=False)
        == "out_of_stock"
    )


def test_dead_beats_low_and_overstock():
    assert _status(closing_stock=5, is_dead=True, days_of_cover=1) == "dead_stock"
    assert _status(closing_stock=5, is_dead=True, days_of_cover=9999) == "dead_stock"


def test_low_stock_below_threshold():
    assert (
        _status(closing_stock=5, is_dead=False, days_of_cover=5, low_stock_days=15) == "low_stock"
    )


def test_overstock_above_threshold():
    assert (
        _status(closing_stock=5, is_dead=False, days_of_cover=200, overstock_days=90) == "overstock"
    )


def test_healthy_in_the_middle():
    assert (
        _status(
            closing_stock=5, is_dead=False, days_of_cover=40, low_stock_days=15, overstock_days=90
        )
        == "healthy"
    )


def test_boundaries_are_exclusive_not_inclusive():
    # days_of_cover == low_stock_days is NOT low stock (strictly less-than);
    # days_of_cover == overstock_days is NOT overstock (strictly greater-than).
    assert (
        _status(
            closing_stock=5, is_dead=False, days_of_cover=15, low_stock_days=15, overstock_days=90
        )
        == "healthy"
    )
    assert (
        _status(
            closing_stock=5, is_dead=False, days_of_cover=90, low_stock_days=15, overstock_days=90
        )
        == "healthy"
    )


def test_custom_thresholds_are_respected():
    assert (
        _status(
            closing_stock=5, is_dead=False, days_of_cover=3, low_stock_days=2, overstock_days=10
        )
        == "healthy"
    )
    assert (
        _status(
            closing_stock=5, is_dead=False, days_of_cover=1, low_stock_days=2, overstock_days=10
        )
        == "low_stock"
    )
