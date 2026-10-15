"""Return correlation. Spec §5, P-003.

The value decides whether to open a second position, so what is checked
is not only the arithmetic but also the behaviour on bad data: gaps,
a constant price, a too short overlap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.risk.correlation import (
    MIN_OVERLAP,
    CorrelationSource,
    correlate,
    log_returns,
    pearson,
    series_correlation,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


def ramp(n: int, start: float, step: float, *, skip: set[int] | None = None):
    skip = skip or set()
    return [
        (T0 + H * i, D(str(round(start + step * i, 8))))
        for i in range(n)
        if i not in skip
    ]


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------
def test_pearson_on_a_perfect_line():
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0
    assert pearson([1.0, 2.0, 3.0], [6.0, 4.0, 2.0]) == -1.0


def test_pearson_refuses_a_degenerate_series():
    """A constant-price series is not independent of its neighbour — it simply
    carries no information. Zero here would mean permission."""
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None
    assert pearson([1.0], [2.0]) is None
    assert pearson([1.0, 2.0], [1.0]) is None


def test_pearson_stays_inside_the_unit_interval():
    """Accumulated floating-point error pushes the result
    beyond the bounds by fractions of epsilon, and −1.0000000000000002 breaks
    the comparison with the threshold."""
    big = [float(i) * 1e8 for i in range(200)]
    assert -1.0 <= pearson(big, [-v for v in big]) <= 1.0
    assert pearson(big, big) <= 1.0


# ---------------------------------------------------------------------------
# Returns
# ---------------------------------------------------------------------------
def test_returns_are_dated_by_the_closing_bar():
    """A return belongs to the transition from the previous bar to the current one.
    Dating it by the start of the period shifts the whole series back."""
    series = ramp(3, 100, 10)
    result = log_returns(series)
    assert set(result) == {T0 + H, T0 + H * 2}
    assert result[T0 + H] == pytest.approx(0.1)


def test_a_gap_breaks_the_chain_instead_of_bridging_it():
    """A return is not computed across a gap: that would be a move over four
    bars passed off as a move over one."""
    series = [(T0, D("100")), (T0 + H, D("110")), (T0 + H * 5, D("121"))]
    result = log_returns(series, H)
    assert set(result) == {T0 + H}


def test_one_gap_does_not_decorrelate_two_identical_series():
    """Regression. Before the fix, one gap in three hundred points dropped
    the correlation of identical series from 1.00 to 0.79 — i.e. moved
    the decision from a certain rejection to permission to open a second position."""
    left = ramp(300, 100, 1)
    right = ramp(300, 100, 1, skip={7, 8, 9})
    value = series_correlation(left, right, min_overlap=10, interval=H)
    assert value is not None
    assert value > D("0.999")


def test_non_positive_prices_reset_the_chain():
    series = [(T0, D("100")), (T0 + H, D("0")), (T0 + H * 2, D("110"))]
    assert log_returns(series) == {}


# ---------------------------------------------------------------------------
# Common grid
# ---------------------------------------------------------------------------
def test_correlation_uses_common_timestamps_only():
    """Correlation by bar index would match different moments:
    a series with a gap would shift relative to its neighbour."""
    left = ramp(300, 100, 1)
    right = ramp(300, 100, 1, skip={7, 8, 9})
    value = series_correlation(left, right, min_overlap=10, interval=H)
    assert value is not None
    assert value > D("0.99")


def test_short_overlap_answers_nothing():
    """P-001: a correlation over a couple dozen points cannot distinguish 0.7,
    and the decision is made exactly by that threshold."""
    left = ramp(30, 100, 1)
    right = ramp(30, 100, 1)
    assert correlate(log_returns(left), log_returns(right), expected=0) is None
    assert MIN_OVERLAP > 30


def test_overlap_must_cover_half_the_window():
    """One hundred twenty points out of seven hundred twenty are not "the same thirty
    days" but two short pieces that happened to coincide in time."""
    left = ramp(200, 100, 1)
    right = ramp(200, 100, 1)
    assert correlate(log_returns(left), log_returns(right), expected=720) is None
    assert correlate(log_returns(left), log_returns(right), expected=300) is not None


def test_disjoint_series_answer_nothing():
    left = ramp(200, 100, 1)
    right = [(T0 + timedelta(days=90) + H * i, D(str(100 + i))) for i in range(200)]
    assert series_correlation(left, right) is None


def test_opposite_moves_give_a_negative_number():
    left = [(T0 + H * i, D(str(100 + (i % 2) * 5))) for i in range(400)]
    right = [(T0 + H * i, D(str(100 - (i % 2) * 5))) for i in range(400)]
    value = series_correlation(left, right, min_overlap=10, interval=H)
    assert value is not None
    assert value < D("-0.9")


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
def test_expected_points_follow_the_timeframe():
    assert CorrelationSource(30, "1h").expected_points == 720
    assert CorrelationSource(30, "4h").expected_points == 180
    assert CorrelationSource(30, "15m").expected_points == 2880


def test_a_symbol_correlates_with_itself_without_touching_the_data():
    source = CorrelationSource(30)

    import asyncio

    assert asyncio.run(source.between("BTCUSDT", "BTCUSDT", T0)) == D(1)


def test_pair_cache_is_order_independent_and_day_scoped():
    """The window slides: yesterday's correlation is a different value today,
    and a shared key would return a stale number."""
    source = CorrelationSource(30)
    source._pairs[("AAAUSDT", "BBBUSDT", T0.date())] = D("0.5")

    import asyncio

    assert asyncio.run(source.between("BBBUSDT", "AAAUSDT", T0)) == D("0.5")
    assert ("AAAUSDT", "BBBUSDT", (T0 + timedelta(days=1)).date()) not in source._pairs


def test_modal_interval_survives_a_hole():
    """The most frequent step is an hour, even when the series has a gap."""
    from src.risk.correlation import modal_interval

    series = ramp(50, 100, 1, skip={20, 21, 22})
    assert modal_interval(series) == H
    assert modal_interval([]) is None


def test_source_refuses_an_unknown_timeframe():
    import pytest as _pytest

    with _pytest.raises(ValueError, match="timeframe"):
        CorrelationSource(30, "3m")
