"""Random entry as the null hypothesis.

A Setup is more than its entry condition. An ATR stop, a 2R target and a holding
window can produce a positive E on their own. This checks
that the comparison really differs in exactly one detail
and that the excess arithmetic is computed over the pooled trades,
not over the spread between seeds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.benchmark import (
    RandomEntryParams,
    advantage_over,
    long_share,
    make_random_signal_fn,
    observed_rate,
)
from src.backtest.engine import MarketBar
from src.strategy.setup_a import build_atr_series
from tests.test_setup_c import BASE_CFG
from tests.test_setup_c import params as setup_params

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H4 = timedelta(hours=4)


def window(n: int = 60) -> list[MarketBar]:
    out = []
    price = 100.0
    for i in range(n):
        price *= 1.001
        out.append(
            MarketBar(
                T0 + H4 * i, D(str(round(price, 6))), D(str(round(price * 1.01, 6))),
                D(str(round(price * 0.99, 6))), D(str(round(price, 6))), D("1"),
            )
        )
    return out


def fire(seed: int, rate: float = 1.0, share: float = 1.0, symbol: str = "AUSDT"):
    p = setup_params()
    bars = window()
    series = build_atr_series(bars, p.stop_atr_period, H4)
    fn = make_random_signal_fn(
        p, RandomEntryParams(signal_rate=rate, long_share=share, seed=seed),
        series, symbol=symbol,
    )
    return p, bars, fn


def test_same_seed_gives_the_same_answer():
    """Otherwise a repeated run gives a different number and there is nothing to compare."""
    _, bars, first = fire(7)
    _, _, second = fire(7)
    a = [first(bars[: i + 1]) for i in range(len(bars))]
    b = [second(bars[: i + 1]) for i in range(len(bars))]
    assert a == b


def test_different_symbols_do_not_share_a_generator():
    """A shared generator would synchronize entries across symbols
    and turn the random entry into a single bet on the whole market."""
    _, bars, a = fire(7, rate=0.5, symbol="AUSDT")
    _, _, b = fire(7, rate=0.5, symbol="BUSDT")
    first = [a(bars[: i + 1]) is not None for i in range(len(bars))]
    second = [b(bars[: i + 1]) is not None for i in range(len(bars))]
    assert first != second


def test_zero_rate_never_fires():
    _, bars, fn = fire(1, rate=0.0)
    assert all(fn(bars[: i + 1]) is None for i in range(len(bars)))


def test_stop_and_target_match_the_setup_construction():
    """Exactly one detail must differ — the entry condition."""
    p, bars, fn = fire(3, rate=1.0, share=1.0)
    series = build_atr_series(bars, p.stop_atr_period, H4)
    signal = None
    for i in range(len(bars)):
        signal = fn(bars[: i + 1])
        if signal is not None:
            atr_value = series.at(bars[i].ts + H4)
            break
    assert signal is not None
    risk = signal.entry - signal.stop
    assert abs(risk - atr_value * p.stop_atr_mult) < D("1e-20")
    assert abs(signal.target - (signal.entry + risk * p.target_r)) < D("1e-20")


def test_stop_cap_applies_to_random_entries_too():
    p = setup_params(max_stop_pct=0.01)
    bars = window()
    series = build_atr_series(bars, p.stop_atr_period, H4)
    fn = make_random_signal_fn(
        p, RandomEntryParams(signal_rate=1.0, long_share=1.0, seed=1), series
    )
    assert all(fn(bars[: i + 1]) is None for i in range(len(bars)))


def test_long_share_drives_direction():
    _, bars, only_long = fire(5, rate=1.0, share=1.0)
    _, _, only_short = fire(5, rate=1.0, share=0.0)
    longs = [s.is_long for i in range(len(bars)) if (s := only_long(bars[: i + 1]))]
    shorts = [s.is_long for i in range(len(bars)) if (s := only_short(bars[: i + 1]))]
    assert longs and all(longs)
    assert shorts and not any(shorts)


# ---------------------------------------------------------------------------
# Excess arithmetic
# ---------------------------------------------------------------------------
def test_advantage_uses_pooled_trades_not_seed_spread():
    """The spread of means between seeds describes the generator's
    instability and ignores each estimate's own error.
    Using it, the excess comes out roughly twice the true one."""
    setup = [0.069] * 500 + [1.0, -1.0] * 250
    noise = [-0.075] * 500 + [1.0, -1.0] * 250
    result = advantage_over(setup, noise)
    assert result is not None
    e_setup, e_random, z = result
    assert e_setup > e_random
    assert 0 < z < 10


def test_advantage_needs_both_samples():
    assert advantage_over([], [1.0, 2.0]) is None
    assert advantage_over([1.0, 2.0], [1.0]) is None


def test_advantage_is_zero_for_identical_samples():
    values = [1.0, -1.0, 2.0, -0.5, 0.3]
    _, _, z = advantage_over(values, list(values))
    assert z == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def test_observed_rate_is_bounded_and_defined_on_empty_input():
    assert observed_rate(0, 0) == 0.0
    assert observed_rate(50, 1000) == 0.05
    assert observed_rate(2000, 1000) == 1.0


def test_long_share_defaults_to_a_coin_flip():
    assert long_share([]) == 0.5


def test_config_is_shared_with_the_setup_tests():
    """The benchmark must read the same parameters as the Setup."""
    assert BASE_CFG["target_r"] == 2.0
    assert setup_params().stop_atr_mult == D("1.5")
