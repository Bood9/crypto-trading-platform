"""Setup A: cascade, VWAP exit and time boundaries.

A separate file, because what is checked is not "does the function work" but three
properties, each of which has already broken silently once:

* ATR from another timeframe must not come from a not-yet-closed hour;
* the cascade window must be ONE for the OI drop, the stop and the VWAP;
* the volume z-score window is taken whole or not at all.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import MarketBar
from src.backtest.indicators import Bar, atr
from src.strategy.setup_a import (
    CASCADE_WINDOW,
    AtrSeries,
    ExitMode,
    SetupAParams,
    build_atr_series,
    cascade_bars,
    cascade_vwap,
    make_signal_fn,
    parse_timeframe,
    volume_zscore,
    warmup_bars,
)
from src.strategy.setup_b import SymbolContext

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)
Q = timedelta(minutes=15)


def bar(ts, o, h, low, c, v="1000") -> MarketBar:
    return MarketBar(ts, D(str(o)), D(str(h)), D(str(low)), D(str(c)), D(str(v)))


# ---------------------------------------------------------------------------
# ATR: the value must be closed by the decision moment
# ---------------------------------------------------------------------------
def hourly(n: int, *, seed: float = 100.0) -> list[MarketBar]:
    out = []
    price = seed
    for i in range(n):
        price += (i % 7) - 3
        out.append(bar(T0 + HOUR * i, price, price + 2, price - 2, price + 1))
    return out


def test_incremental_atr_matches_the_reference_at_every_point():
    """A second implementation of the same formula must agree with the first,
    otherwise it is of no use: just a second version of the error."""
    bars = hourly(60)
    series = build_atr_series(bars, 14, HOUR)

    assert len(series.values) == len(bars) - 13
    for j, value in enumerate(series.values, start=13):
        reference = atr([Bar(b.high, b.low, b.close) for b in bars[: j + 1]], 14)
        assert reference is not None
        assert value == reference, f"mismatch at bar {j}"


def test_atr_series_is_keyed_by_close_not_by_open():
    """The series key is the close time of the hourly bar.

    Candles are dated by the period start, so a key by open time would give
    a 15-minute entry the ATR of an hour that is still in progress.
    """
    bars = hourly(30)
    series = build_atr_series(bars, 14, HOUR)
    assert series.closes[0] == bars[13].ts + HOUR


def test_atr_at_the_moment_of_decision_ignores_the_unfinished_hour():
    """Look-ahead regression.

    Corrupting an hourly bar that is not yet closed at the decision moment
    must not change the ATR given at that moment. The error does not crash
    and is not logged: it just makes the stop more precise than possible.
    """
    bars = hourly(30)
    # The decision is made at the close of the 15-minute bar 20:00-20:15,
    # i.e. inside the hour 20:00-21:00, which is still in progress.
    current_hour = 20
    decided_at = bars[current_hour].ts + Q

    clean = build_atr_series(bars, 14, HOUR).at(decided_at)

    spoiled = list(bars)
    hot = spoiled[current_hour]
    spoiled[current_hour] = MarketBar(
        hot.ts, hot.open, hot.high * 50, hot.low / 50, hot.close, hot.volume
    )
    dirty = build_atr_series(spoiled, 14, HOUR).at(decided_at)

    assert clean is not None
    assert clean == dirty, "ATR taken from a not-yet-closed hour"

    # And at the close of that hour the same bar is already visible; otherwise the check
    # would pass simply because the value never updates.
    assert build_atr_series(spoiled, 14, HOUR).at(hot.ts + HOUR) != clean


def test_atr_series_restarts_after_a_non_numeric_bar():
    """A non-numeric bar resets the accumulation instead of poisoning
    the recurrent sum for the rest of the series."""
    bars = hourly(40)
    bars[20] = MarketBar(bars[20].ts, D("NaN"), D("NaN"), D("NaN"), D("NaN"))
    series = build_atr_series(bars, 14, HOUR)

    assert all(v.is_finite() and v > 0 for v in series.values)
    assert all(close <= bars[20].ts or close >= bars[33].ts + HOUR for close in series.closes)


def test_empty_series_answers_nothing_rather_than_zero():
    series = AtrSeries(closes=[], values=[])
    assert series.at(T0) is None


# ---------------------------------------------------------------------------
# Cascade window
# ---------------------------------------------------------------------------
def test_cascade_covers_exactly_the_compression_window():
    """On a 15-minute grid an hourly window is four bars
    including the entry bar."""
    window = [bar(T0 + Q * i, 10, 11, 9, 10) for i in range(12)]
    current = window[-1]
    picked = cascade_bars(window, current.ts, CASCADE_WINDOW)

    assert len(picked) == 4
    assert picked[-1] is current
    assert picked[0].ts == current.ts - Q * 3


def test_cascade_is_measured_in_time_not_in_bars():
    """A gap in the series must not stretch the window: "the last four
    bars" on a gappy stretch are no longer an hour."""
    stamps = [T0, T0 + Q, T0 + timedelta(hours=3), T0 + timedelta(hours=3, minutes=15)]
    window = [bar(ts, 10, 11, 9, 10) for ts in stamps]
    picked = cascade_bars(window, window[-1].ts, CASCADE_WINDOW)

    assert [b.ts for b in picked] == stamps[2:]


def test_cascade_of_a_single_bar_when_bars_are_wider_than_the_window():
    window = [bar(T0 + timedelta(hours=4) * i, 10, 11, 9, 10) for i in range(5)]
    assert len(cascade_bars(window, window[-1].ts, CASCADE_WINDOW)) == 1


# ---------------------------------------------------------------------------
# Cascade VWAP
# ---------------------------------------------------------------------------
def test_vwap_weights_typical_price_by_volume():
    bars = [
        bar(T0, 10, 12, 6, 9, v="1"),      # typical (12+6+9)/3 = 9
        bar(T0 + Q, 10, 30, 15, 15, v="3"),  # typical (30+15+15)/3 = 20
    ]
    # (9*1 + 20*3) / 4 = 17.25
    assert cascade_vwap(bars) == D("17.25")


def test_vwap_without_volume_answers_nothing_rather_than_averaging():
    """P-001: falling back to a simple mean would pass off "no volume"
    as "even volume", while the whole point of the value is the volume distribution."""
    assert cascade_vwap([bar(T0, 10, 11, 9, 10, v="0")]) is None
    assert cascade_vwap([]) is None
    assert cascade_vwap([MarketBar(T0, D(1), D(1), D(1), D(1), D("NaN"))]) is None


# ---------------------------------------------------------------------------
# Volume z-score
# ---------------------------------------------------------------------------
def test_volume_zscore_refuses_a_truncated_window():
    """A truncated window is a different statistic under the same name."""
    bars = [bar(T0 + Q * i, 10, 11, 9, 10, v=str(100 + i)) for i in range(30)]
    assert volume_zscore(bars, 100) is None
    assert volume_zscore(bars, 20) is not None


def test_zscore_window_is_counted_on_the_entry_timeframe():
    """Seven days on a 15-minute grid is 672 bars, not 168.
    Counting in hours gave a window four times shorter than the config states."""
    params = make_params(entry_timeframe="15m", volume_zscore_lookback_days=7)
    assert params.volume_zscore_lookback_bars == 672
    assert replace(params, entry_timeframe="1h").volume_zscore_lookback_bars == 168
    assert warmup_bars(params) == 673


def test_parse_timeframe_rejects_nonsense():
    assert parse_timeframe("15m") == Q
    assert parse_timeframe("4h") == timedelta(hours=4)
    for bad in ("15", "m15", "x", "1w", ""):
        with pytest.raises(ValueError):
            parse_timeframe(bad)


# ---------------------------------------------------------------------------
# The whole signal
# ---------------------------------------------------------------------------
def make_params(**overrides) -> SetupAParams:
    base = {
        "oi_drop_pct_1h": 3.0,
        "candle_range_atr_mult": 1.5,
        "require_direction_match": True,
        "volume_zscore_min": 2.0,
        "volume_zscore_lookback_days": 1,
        "funding_upper_percentile": 90,
        "funding_lower_percentile": 10,
        "funding_percentile_lookback_days": 30,
        "entry_timeframe": "1h",
        "stop_atr_mult": 1.5,
        "stop_atr_period": 14,
        "exit_mode": "vwap",
        "target_r": 2.5,
        "time_stop_hours": 24,
        "oi_drop_pct_1h_sensitivity": [3.0, 5.0, 8.0, 12.0],
        "candle_range_atr_mult_sensitivity": [0.5, 1.0, 1.5, 2.0],
        "volume_zscore_min_sensitivity": [1.0, 1.5, 2.0, 2.5],
        "funding_percentile_sensitivity": [10, 20, 30, 40],
    }
    plain = {k: v for k, v in overrides.items() if k in base}
    base.update(plain)
    params = SetupAParams.from_config(base)
    rest = {k: v for k, v in overrides.items() if k not in plain}
    return replace(params, **rest) if rest else params


def cascade_window_for_long(params: SetupAParams) -> list[MarketBar]:
    """A series ending with a crash bar: volume spikes, price drops."""
    need = warmup_bars(params)
    # Volume wanders slightly: on a perfectly flat series sigma is zero,
    # and the z-score honestly refuses to compute.
    window = [
        bar(T0 + HOUR * i, 100, 101, 99, 100, v=str(100 + i % 5))
        for i in range(need - 1)
    ]
    crash = T0 + HOUR * (need - 1)
    window.append(bar(crash, 100, 100, 80, 82, v="100000"))
    return window


def context_for_long(window: list[MarketBar]) -> SymbolContext:
    moment = window[-1].ts
    return SymbolContext(
        oi_ts=[moment - HOUR, moment],
        oi_values=[D("1000"), D("900")],  # 10% drop
        funding_ts=[moment - timedelta(days=d) for d in range(30, 0, -1)],
        funding_rates=[D("0.01")] * 29 + [D("-0.05")],  # low → long
    )


def test_long_signal_targets_the_cascade_vwap():
    params = make_params()
    window = cascade_window_for_long(params)
    series = build_atr_series(window, params.stop_atr_period, HOUR)
    signal = make_signal_fn(context_for_long(window), params, series)(window)

    assert signal is not None and signal.is_long
    # A cascade on the hourly grid is one entry bar.
    assert signal.target == cascade_vwap([window[-1]])
    # The stop is beyond the cascade extreme, one ATR below it.
    assert signal.stop < window[-1].low
    assert signal.entry < signal.target < window[-1].high


def test_stop_is_placed_behind_the_whole_cascade_not_the_last_bar():
    """The extreme of the event, not the extreme of the event's last candle."""
    params = make_params(entry_timeframe="15m", volume_zscore_lookback_days=1)
    need = warmup_bars(params)
    window = [
        bar(T0 + Q * i, 100, 101, 99, 100, v=str(100 + i % 5))
        for i in range(need - 4)
    ]
    base = T0 + Q * (need - 4)
    # The cascade bottom is on its first bar, not on the entry bar.
    window += [
        bar(base, 100, 100, 70, 75, v="100000"),          # crash, low 70
        bar(base + Q, 75, 85, 74, 84, v="90000"),            # bounce
        bar(base + Q * 2, 84, 86, 83, 85, v="80000"),
        bar(base + Q * 3, 85, 86, 76, 77, v="100000"),       # entry bar, low 76
    ]
    series = build_atr_series(
        [bar(T0 + HOUR * i, 100, 101, 99, 100) for i in range(40)],
        params.stop_atr_period,
        HOUR,
    )
    ctx = context_for_long(window)
    signal = make_signal_fn(ctx, params, series)(window)

    assert signal is not None
    lowest = min(b.low for b in window[-4:])
    assert lowest == D("70")
    assert signal.stop < lowest
    assert signal.stop < window[-1].low - D("5"), "stop computed from a single bar"


def test_signal_is_dropped_when_vwap_is_already_behind_the_entry():
    """A target behind the entry means there is nowhere to return to: the trade
    would degenerate into an instant exit on the very first bar."""
    params = make_params()
    window = cascade_window_for_long(params)
    # Close above the typical price: the VWAP ends up BEHIND the long.
    window[-1] = bar(window[-1].ts, 100, 100, 80, 99.5, v="100000")
    series = build_atr_series(window, params.stop_atr_period, HOUR)
    signal = make_signal_fn(context_for_long(window), params, series)(window)

    assert signal is None


def test_fixed_r_mode_still_available_for_comparison():
    params = make_params(exit_mode="fixed_r", target_r=2.5)
    window = cascade_window_for_long(params)
    series = build_atr_series(window, params.stop_atr_period, HOUR)
    signal = make_signal_fn(context_for_long(window), params, series)(window)

    assert signal is not None
    risk = signal.entry - signal.stop
    assert signal.target == signal.entry + risk * D("2.5")


def test_unknown_exit_mode_is_refused_at_load():
    """P-001: an unknown config value is an error at startup,
    not a silent fallback to the default."""
    with pytest.raises(ValueError, match="exit_mode"):
        make_params(exit_mode="trailing")


def _minimal_cfg(**overrides) -> dict:
    cfg = {
        "oi_drop_pct_1h": 3.0, "candle_range_atr_mult": 1.5,
        "require_direction_match": True, "volume_zscore_min": 2.0,
        "volume_zscore_lookback_days": 7, "funding_upper_percentile": 90,
        "funding_lower_percentile": 10, "funding_percentile_lookback_days": 30,
        "stop_atr_mult": 1.5, "stop_atr_period": 14, "target_r": 2.5,
        "time_stop_hours": 24,
        "oi_drop_pct_1h_sensitivity": [3.0, 5.0, 8.0, 12.0],
        "candle_range_atr_mult_sensitivity": [0.5, 1.0, 1.5, 2.0],
        "volume_zscore_min_sensitivity": [1.0, 1.5, 2.0, 2.5],
        "funding_percentile_sensitivity": [10, 20, 30, 40],
    }
    cfg.update(overrides)
    return cfg


def test_exit_mode_defaults_to_vwap_when_absent():
    assert SetupAParams.from_config(_minimal_cfg()).exit_mode is ExitMode.VWAP


def test_knob_without_a_sweep_is_refused_at_load():
    """A knob without a sweep is a knob whose sensitivity
    nobody checked. DEC-015 showed how that ends: the universe
    width bound the composition, and the guard did not count it as a knob."""
    cfg = _minimal_cfg()
    del cfg["candle_range_atr_mult_sensitivity"]
    with pytest.raises(ValueError, match="has no sweep"):
        SetupAParams.from_config(cfg)


def test_base_value_outside_its_own_sweep_is_refused():
    """A sweep around a number that is not in it cannot be compared
    with anything."""
    with pytest.raises(ValueError, match="is missing from its own"):
        SetupAParams.from_config(
            _minimal_cfg(volume_zscore_min_sensitivity=[1.0, 1.5, 3.0])
        )
