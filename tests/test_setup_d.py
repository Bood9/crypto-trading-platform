"""Setup D: time-series momentum on daily bars. DEC-033.

Checked against the preregistration: the sign of the window return
sets the direction, there is no target, the stop is a size instrument with a ceiling,
the null hypothesis differs only in direction.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import MarketBar
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_d import (
    SetupDParams,
    lookback_return,
    make_signal_fn,
    warmup_bars,
)

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2023, 1, 1, tzinfo=UTC)

CFG = {
    "lookback_days": 28,
    "hold_days": 7,
    "stop_atr_mult": 2.0,
    "stop_atr_period": 14,
    "max_stop_pct": 12.0,
    "lookback_days_sensitivity": [7, 14, 28, 56],
    "hold_days_sensitivity": [3, 7, 14],
    "stop_atr_mult_sensitivity": [1.5, 2.0, 3.0],
}


def params(**overrides) -> SetupDParams:
    cfg = dict(CFG)
    cfg.update({k: v for k, v in overrides.items() if k in cfg})
    built = SetupDParams.from_config(cfg)
    rest = {k: v for k, v in overrides.items() if k not in cfg}
    return replace(built, **rest) if rest else built


def trend(n: int, *, drift: float, noise: float = 0.01, seed: int = 1) -> list[MarketBar]:
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        price *= 1 + drift + rng.gauss(0, noise)
        hi, lo = price * 1.01, price * 0.99
        out.append(
            MarketBar(T0 + DAY * i, D(f"{price:.6f}"), D(f"{hi:.6f}"),
                      D(f"{lo:.6f}"), D(f"{price:.6f}"), D("1"))
        )
    return out


def fire(bars, p=None, **kw):
    p = p or params()
    series = build_atr_series(bars, p.stop_atr_period, DAY)
    return make_signal_fn(p, series, **kw)(bars)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
def test_loaded_values_match_the_preregistration():
    """DEC-033: the values were fixed before the code and do not change."""
    p = params()
    assert (p.lookback_days, p.hold_days) == (28, 7)
    assert p.stop_atr_mult == D("2.0")
    assert p.max_stop_pct == D("12.0")
    assert p.time_stop_hours == 168


def test_every_knob_needs_a_sweep():
    for knob in ("lookback_days_sensitivity", "hold_days_sensitivity",
                 "stop_atr_mult_sensitivity"):
        cfg = dict(CFG)
        del cfg[knob]
        with pytest.raises(ValueError, match="has no sweep"):
            SetupDParams.from_config(cfg)


# ---------------------------------------------------------------------------
# Window return
# ---------------------------------------------------------------------------
def test_lookback_is_measured_by_time_not_by_bar_count():
    """A dropped incomplete day would shift the bar index."""
    bars = trend(40, drift=0.0)
    gapped = bars[:20] + bars[21:]  # one day dropped out
    current = gapped[-1]
    got = lookback_return(gapped, 28)
    expected_base = next(b for b in bars if b.ts == current.ts - DAY * 28)
    assert got == current.close / expected_base.close - 1


def test_missing_anchor_day_answers_nothing():
    """P-001: no bar exactly on the required date means no answer, not a neighbour."""
    bars = trend(40, drift=0.0)
    anchor = bars[-1].ts - DAY * 28
    holed = [b for b in bars if b.ts != anchor]
    assert lookback_return(holed, 28) is None


def test_short_history_answers_nothing():
    assert lookback_return(trend(10, drift=0.0), 28) is None


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------
def test_rising_market_gives_a_long_without_target():
    signal = fire(trend(60, drift=0.01, noise=0.002))
    assert signal is not None and signal.is_long
    assert signal.target is None, "time exit only"


def test_falling_market_gives_a_short():
    signal = fire(trend(60, drift=-0.01, noise=0.002))
    assert signal is not None and not signal.is_long
    assert signal.stop > signal.entry


def test_stop_is_two_atr_taken_at_the_close_of_the_day():
    bars = trend(60, drift=0.01, noise=0.002)
    p = params()
    series = build_atr_series(bars, p.stop_atr_period, DAY)
    signal = fire(bars, p)
    atr_value = series.at(bars[-1].ts + DAY)
    assert abs((signal.entry - signal.stop) - atr_value * D("2.0")) < D("1e-20")


def test_stop_beyond_the_cap_is_refused_in_the_setup():
    """DEC-015: cut in the setup, not silently in the Risk Manager."""
    bars = trend(60, drift=0.01, noise=0.002)
    assert fire(bars, params(max_stop_pct=0.1)) is None


def test_warmup_covers_lookback_and_atr():
    assert warmup_bars(params()) == 29
    assert fire(trend(20, drift=0.01)) is None


# ---------------------------------------------------------------------------
# Null hypothesis
# ---------------------------------------------------------------------------
def test_coin_flip_changes_only_the_direction():
    """Same entries, different direction. Everything else is shared."""
    bars = trend(60, drift=0.01, noise=0.002)
    real = fire(bars)
    flips = [fire(bars, coin_flip_seed=f"s{i}") for i in range(40)]
    assert all(f is not None for f in flips), "the coin must not cancel the entry"
    assert {f.is_long for f in flips} == {True, False}
    for f in flips:
        assert f.entry == real.entry
        assert abs(f.entry - f.stop) == abs(real.entry - real.stop)


def test_coin_flip_is_reproducible_by_seed():
    bars = trend(60, drift=0.01, noise=0.002)
    assert fire(bars, coin_flip_seed="x") == fire(bars, coin_flip_seed="x")


# ---------------------------------------------------------------------------
# Look-ahead (spec §14)
# ---------------------------------------------------------------------------
def test_corrupting_the_future_does_not_change_past_signals():
    bars = trend(120, drift=0.003, noise=0.02, seed=5)
    p = params()
    cut = 80

    def signals(series_bars):
        atr_series = build_atr_series(series_bars, p.stop_atr_period, DAY)
        fn = make_signal_fn(p, atr_series)
        return [
            (series_bars[i].ts, s.is_long, s.entry, s.stop)
            for i in range(len(series_bars))
            if (s := fn(series_bars[: i + 1])) is not None
        ]

    rng = random.Random(9)
    spoiled = list(bars)
    for i in range(cut + 1, len(spoiled)):
        wild = D(f"{rng.uniform(1, 10000):.6f}")
        spoiled[i] = MarketBar(bars[i].ts, wild, wild * 2, wild / 2, wild, D("1"))

    horizon = bars[cut].ts
    clean = [s for s in signals(bars) if s[0] <= horizon]
    dirty = [s for s in signals(spoiled) if s[0] <= horizon]
    assert clean, "no signals arose before the corruption point"
    assert clean == dirty
