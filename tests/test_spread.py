"""Tests for the diagnostic spread estimate (DEC-007).

The value affects nothing: it takes no part in universe selection
and is not part of the setup definitions. The tests check that the estimate honestly
reports its own unfitness rather than substituting a plausible number.
"""

from __future__ import annotations

import random

import pytest

from src.core.config import load_settings
from src.strategy.spread import (
    SpreadEstimate,
    abdi_ranaldo_spread,
    describe_distribution,
)


def synthetic_series(
    n: int,
    *,
    spread_pct: float,
    range_noise: float = 0.001,
    drift_vol: float = 0.004,
    seed: int = 7,
):
    """Microstructure simulation: the effective price plus quote bounce.

    `range_noise` is how far the midpoint (high+low)/2 misses the
    effective price. The parameter is exposed not for looks: whether
    the spread is distinguishable at all depends on it directly. The method estimates
    a covariance of order s²/4, and if the midpoint noise is comparable
    to that value, the estimate degenerates into noise.
    """
    rng = random.Random(seed)
    price = 100.0
    highs, lows, closes = [], [], []
    half = spread_pct / 100 / 2
    for _ in range(n):
        price *= 1 + rng.gauss(0, drift_vol)
        # The bar range around the effective price.
        span = abs(rng.gauss(0, range_noise)) + half
        skew = rng.gauss(0, range_noise)
        highs.append(price * (1 + span + skew))
        lows.append(price * (1 - span + skew))
        # The close is the bid or the ask, i.e. a bounce of half the spread.
        closes.append(price * (1 + skew + half * rng.choice((-1, 1))))
    return highs, lows, closes


def test_estimate_is_positive_when_spread_dominates_noise():
    h, low, c = synthetic_series(2000, spread_pct=1.0, range_noise=0.0005)
    result = abdi_ranaldo_spread(h, low, c, symbol="SOLUSDT")

    assert result.spread_pct is not None
    assert result.spread_pct > 0
    assert result.reliable


def test_wider_spread_gives_larger_estimate():
    """Order of magnitude beats precision: diagnostics look for outliers, not a value."""
    narrow = abdi_ranaldo_spread(
        *synthetic_series(3000, spread_pct=0.5, range_noise=0.0005, seed=3)
    )
    wide = abdi_ranaldo_spread(
        *synthetic_series(3000, spread_pct=2.0, range_noise=0.0005, seed=3)
    )

    assert narrow.spread_pct is not None
    assert wide.spread_pct is not None
    assert wide.spread_pct > narrow.spread_pct


def test_estimate_is_unstable_when_noise_swamps_spread():
    """A limit of the method's applicability, not an implementation defect.

    When the range midpoint noise is comparable to s²/4, the estimate stops
    being reproducible: on different stretches of the same process
    it jumps several-fold, and some stretches give no result at all.

    We check exactly the spread across seeds, not a specific outcome of one
    run: a claim "with seed=11 it returns None" would break
    from any edit to the generator, saying nothing about the code.

    Practical meaning: on 15-minute bars of liquid perps the method
    does not have to work. Its numbers are read only together with `reliable`.
    """
    results = [
        abdi_ranaldo_spread(
            *synthetic_series(
                400, spread_pct=0.1, range_noise=0.02, drift_vol=0.02, seed=s
            )
        )
        for s in range(30)
    ]
    values = [r.spread_pct for r in results if r.spread_pct is not None]

    # Either some runs degenerate, or the estimates diverge many-fold;
    # in both cases the value cannot be trusted.
    degenerate = len(values) < len(results)
    scattered = bool(values) and max(values) > 3 * min(values)
    assert degenerate or scattered


def test_short_series_is_reported_as_unreliable():
    result = abdi_ranaldo_spread([1.0, 1.1], [0.9, 1.0], [1.0, 1.05])
    assert result.spread_pct is None
    assert not result.reliable


def test_negative_covariance_returns_none_not_zero():
    """Zero would read as "no spread", the opposite of the truth.

    Negative covariance is normal for this method: on a quiet series noise
    overrides the signal. Replacing it with zero would be lying (P-001).
    """
    # A fully deterministic series without microstructure noise.
    n = 100
    closes = [100.0 + i for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]

    result = abdi_ranaldo_spread(highs, lows, closes)

    assert result.spread_pct is None
    assert not result.reliable


def test_mismatched_series_lengths_are_rejected():
    with pytest.raises(ValueError, match="the same length"):
        abdi_ranaldo_spread([1.0, 2.0], [0.5], [1.0, 2.0])


def test_zero_and_negative_prices_are_discarded():
    """Collapses like LUNA give zeros, where the logarithm is undefined."""
    h, low, c = synthetic_series(200, spread_pct=0.10)
    h[10] = 0.0
    low[20] = -1.0
    c[30] = 0.0

    result = abdi_ranaldo_spread(h, low, c)

    assert result.discarded_share > 0
    assert result.sample_size == 197


def test_mostly_broken_series_is_unreliable():
    highs = [0.0] * 90 + [101.0] * 10
    lows = [0.0] * 90 + [99.0] * 10
    closes = [0.0] * 90 + [100.0] * 10

    result = abdi_ranaldo_spread(highs, lows, closes)

    assert not result.reliable


def test_inverted_high_low_is_discarded():
    """high below low is a corrupted bar, not a narrow range."""
    h, low, c = synthetic_series(100, spread_pct=0.10)
    h[5], low[5] = low[5], h[5]

    result = abdi_ranaldo_spread(h, low, c)
    assert result.discarded_share > 0


# ---------------------------------------------------------------------------
# Distribution across the universe
# ---------------------------------------------------------------------------
def test_distribution_reports_quantiles():
    estimates = [
        SpreadEstimate(f"S{i}", spread_pct=float(i), sample_size=100) for i in range(1, 101)
    ]
    summary = describe_distribution(estimates)

    assert summary["count"] == 100
    assert summary["median"] == pytest.approx(50.5)
    assert summary["min"] == 1.0
    assert summary["max"] == 100.0


def test_distribution_counts_unreliable_separately():
    estimates = [
        SpreadEstimate("A", spread_pct=0.05, sample_size=100),
        SpreadEstimate("B", spread_pct=None, sample_size=100),
        SpreadEstimate("C", spread_pct=0.07, sample_size=5),  # sample too small
    ]
    summary = describe_distribution(estimates)

    assert summary["count"] == 1
    assert summary["unreliable"] == 2


def test_distribution_on_empty_input():
    assert describe_distribution([])["count"] == 0


def test_outlier_is_visible_in_the_tail():
    """The point of the diagnostics is to see an outlier, not to filter it out."""
    normal = [SpreadEstimate(f"S{i}", spread_pct=0.05, sample_size=100) for i in range(50)]
    outlier = SpreadEstimate("THINUSDT", spread_pct=3.0, sample_size=100)

    summary = describe_distribution([*normal, outlier])

    assert summary["median"] == pytest.approx(0.05)
    assert summary["max"] == 3.0


# ---------------------------------------------------------------------------
# DEC-007: spread removed from selection
# ---------------------------------------------------------------------------
def test_universe_config_has_no_spread_threshold():
    """A dead threshold in the config is worse than a missing one: it looks functional."""
    universe = load_settings().universe
    assert not hasattr(universe, "max_median_spread_pct")


def test_slippage_sweep_is_configured():
    costs = load_settings().costs
    assert costs.slippage_sensitivity == [0.02, 0.05, 0.10, 0.20]


def test_base_slippage_is_inside_the_sweep():
    """Otherwise the base run would fall outside the sweep and there is nothing to compare."""
    costs = load_settings().costs
    assert costs.slippage_pct in costs.slippage_sensitivity


def test_sweep_covers_an_order_of_magnitude():
    """The sweep must reach the values at which the strategy breaks."""
    sweep = load_settings().costs.slippage_sensitivity
    assert max(sweep) / min(sweep) >= 5
