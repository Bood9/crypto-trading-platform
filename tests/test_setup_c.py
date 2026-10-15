"""Setup C. Spec §6, DEC-018.

Same hypothesis as B; one difference: an ATR stop with a hard ceiling
instead of a structural extreme. That is what is checked first:
everything else is inherited and already covered by Setup B's tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import MarketBar
from src.backtest.funnel import Funnel
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_b import SymbolContext
from src.strategy.setup_c import (
    SETUP_C_STAGES,
    SetupCParams,
    make_signal_fn,
    warmup_bars,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H4 = timedelta(hours=4)


BASE_CFG = {
    "breakout_lookback": 20,
    "require_oi_rising": True,
    "oi_lookback_hours": 48,
    "funding_min_percentile": 30,
    "funding_max_percentile": 70,
    "funding_percentile_lookback_days": 30,
    "entry_timeframe": "4h",
    "stop_atr_mult": 1.5,
    "stop_atr_period": 14,
    "max_stop_pct": 8.0,
    "target_r": 2.0,
    "time_stop_hours": 168,
    "breakout_lookback_sensitivity": [10, 20, 40, 60],
    "stop_atr_mult_sensitivity": [1.0, 1.5, 2.0, 3.0],
    "funding_band_sensitivity": [20, 30, 40],
    "time_stop_hours_sensitivity": [48, 96, 168, 336],
    "target_r_sensitivity": [2.0, 2.5, 3.0],
}


def params(**overrides) -> SetupCParams:
    cfg = dict(BASE_CFG)
    plain = {k: v for k, v in overrides.items() if k in cfg}
    cfg.update(plain)
    built = SetupCParams.from_config(cfg)
    rest = {k: v for k, v in overrides.items() if k not in plain}
    return replace(built, **rest) if rest else built


def bar(i: int, o, h, low, c, v="1000") -> MarketBar:
    return MarketBar(
        T0 + H4 * i, D(str(o)), D(str(h)), D(str(low)), D(str(c)), D(str(v))
    )


def breakout_window(p: SetupCParams, *, span: float = 1.0, close: float = 130.0):
    """A series ending with a breakout of the 20-bar high.

    `span` controls the bar range, i.e. the ATR, and through it
    the distance to the stop.
    """
    need = warmup_bars(p)
    window = [
        bar(i, 100, 100 + span, 100 - span, 100) for i in range(need + 5)
    ]
    window.append(bar(len(window), 100, close, 99, close))
    return window


def context_for(window) -> SymbolContext:
    moment = window[-1].ts
    return SymbolContext(
        oi_ts=[moment - timedelta(hours=48), moment],
        oi_values=[D("1000"), D("1200")],  # rising
        funding_ts=[moment - timedelta(days=d) for d in range(30, 0, -1)],
        # Half below, half above → rank about 50, the zone is neutral.
        funding_rates=[D("0.001")] * 15 + [D("0.02")] * 14 + [D("0.01")],
    )


def signal_for(window, p: SetupCParams, funnel: Funnel | None = None):
    series = build_atr_series(window, p.stop_atr_period, H4)
    return make_signal_fn(context_for(window), p, series, funnel)(window)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------
def test_every_knob_needs_a_sweep():
    for knob in (
        "breakout_lookback_sensitivity",
        "stop_atr_mult_sensitivity",
        "funding_band_sensitivity",
        "time_stop_hours_sensitivity",
        "target_r_sensitivity",
    ):
        cfg = dict(BASE_CFG)
        del cfg[knob]
        with pytest.raises(ValueError, match="has no sweep"):
            SetupCParams.from_config(cfg)


def test_base_value_must_sit_inside_its_own_sweep():
    cfg = dict(BASE_CFG, target_r=2.25)
    with pytest.raises(ValueError, match="is missing"):
        SetupCParams.from_config(cfg)


def test_holding_window_is_longer_than_the_signal_window():
    """A rule written down before measuring: the holding window is no shorter than
    the one the signal is defined over. B had 12 bars with a 20-bar
    signal: the trade got three times less time than it took
    to detect it."""
    p = params()
    assert p.time_stop_bars == 42
    assert p.time_stop_bars > p.breakout_lookback * 2


def test_target_sweep_never_drops_below_the_p006_floor():
    """The target is pinned to the economic floor, not to reachability."""
    p = params()
    assert min(p.target_r_sensitivity) >= D("2.0")
    assert p.target_r == D("2.0")


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------
def test_long_breakout_produces_an_atr_stop():
    p = params()
    window = breakout_window(p)
    signal = signal_for(window, p)

    assert signal is not None and signal.is_long
    risk = signal.entry - signal.stop
    assert risk > 0
    # The stop is exactly 1.5 ATR, not beyond the structural extreme.
    # Compared with a tolerance: ATR is the result of dividing by 14, and the price
    # difference is rounded to the Decimal context precision.
    series = build_atr_series(window, p.stop_atr_period, H4)
    atr_value = series.at(window[-1].ts + H4)
    assert abs(risk - atr_value * p.stop_atr_mult) < D("1e-20")
    assert abs(signal.target - (signal.entry + risk * p.target_r)) < D("1e-20")


def test_short_breakout_mirrors_the_long_one():
    p = params()
    need = warmup_bars(p)
    window = [bar(i, 100, 101, 99, 100) for i in range(need + 5)]
    # A downward breakout, but not so sharp that the ATR pushes the stop
    # past the eight-percent ceiling: that is checked by a separate test.
    window.append(bar(len(window), 100, 100, 94, 95))
    signal = signal_for(window, p)

    assert signal is not None and not signal.is_long
    assert signal.stop > signal.entry
    assert signal.target < signal.entry


def test_no_breakout_no_signal():
    p = params()
    window = breakout_window(p, close=100.5)
    assert signal_for(window, p) is None


def test_falling_oi_blocks_the_signal():
    p = params()
    window = breakout_window(p)
    ctx = context_for(window)
    ctx.oi_values = [D("1200"), D("1000")]
    series = build_atr_series(window, p.stop_atr_period, H4)
    assert make_signal_fn(ctx, p, series)(window) is None


def test_unknown_oi_is_not_treated_as_rising():
    """P-001: no data, no confirmation."""
    p = params()
    window = breakout_window(p)
    ctx = context_for(window)
    ctx.oi_ts, ctx.oi_values = [], []
    series = build_atr_series(window, p.stop_atr_period, H4)
    assert make_signal_fn(ctx, p, series)(window) is None


def test_extreme_funding_blocks_the_signal():
    """Neutral funding is what tells a fresh trend from an overheated crowd."""
    p = params()
    window = breakout_window(p)
    ctx = context_for(window)
    ctx.funding_rates = [D("0.001")] * 29 + [D("9.0")]  # rank ~98
    series = build_atr_series(window, p.stop_atr_period, H4)
    assert make_signal_fn(ctx, p, series)(window) is None


def test_missing_atr_blocks_the_signal():
    p = params()
    window = breakout_window(p)
    assert make_signal_fn(context_for(window), p, None)(window) is None


# ---------------------------------------------------------------------------
# Stop distance ceiling: the only substantive difference from B
# ---------------------------------------------------------------------------
def test_far_stop_is_rejected_by_the_setup_not_by_the_risk_manager():
    """DEC-015: a Risk Manager rejection is not visible in the setup logic and silently
    skews the sample toward narrow stops. The cut must happen here."""
    p = params()
    # Range 10 at price 100 → ATR about 10%, stop 1.5×10% = 15% > 8%.
    window = breakout_window(p, span=10.0, close=140.0)
    assert signal_for(window, p) is None

    # The same series with a raised ceiling gives a signal, so it was
    # the ceiling that cut it, not something else.
    assert signal_for(window, replace(p, max_stop_pct=D("50"))) is not None


def test_stop_cap_is_measured_in_percent_of_entry():
    p = params(max_stop_pct=5.0)
    window = breakout_window(p, span=1.0)
    signal = signal_for(window, p)
    assert signal is not None
    distance_pct = (signal.entry - signal.stop) / signal.entry * 100
    assert distance_pct <= D("5")


# ---------------------------------------------------------------------------
# Funnel
# ---------------------------------------------------------------------------
def test_funnel_counters_stay_monotone():
    p = params()
    funnel = Funnel(stages=SETUP_C_STAGES)
    window = breakout_window(p)
    signal_for(window, p, funnel)
    assert funnel.is_monotone()
    assert funnel.counts["signal"] == 1


def test_funnel_marks_the_stage_where_the_candidate_died():
    p = params()
    funnel = Funnel(stages=SETUP_C_STAGES)
    window = breakout_window(p, span=10.0, close=140.0)
    assert signal_for(window, p, funnel) is None
    assert funnel.counts["atr_known"] == 1
    assert funnel.counts["stop_inside_cap"] == 0
    assert funnel.counts["signal"] == 0


def test_funnel_is_optional_and_does_not_change_the_signal():
    p = params()
    window = breakout_window(p)
    without = signal_for(window, p)
    with_funnel = signal_for(window, p, Funnel(stages=SETUP_C_STAGES))
    assert without == with_funnel


# ---------------------------------------------------------------------------
# Look-ahead. Spec §14
# ---------------------------------------------------------------------------
def test_precomputed_atr_series_cannot_leak_the_future():
    """The ATR series is built over the WHOLE bar array up front: exactly where
    Setup A's look-ahead hid (DEC-014).

    Corrupting bars after the decision point must not change any signal
    before it. The error does not crash and is not logged: it just makes stops
    more precise than possible.
    """
    import random

    p = params()
    rng = random.Random(4)
    need = warmup_bars(p)
    clean: list[MarketBar] = []
    price = 100.0
    for i in range(need + 60):
        price *= 1 + rng.gauss(0, 0.01)
        clean.append(
            bar(i, price, price * 1.01, price * 0.99, price * (1 + rng.gauss(0, 0.004)))
        )

    # Context for the whole series: OI rises monotonically, funding is neutral
    # at every point. Otherwise no signals arise at all and there is nothing
    # to check.
    span = [b.ts for b in clean]
    wide_context = SymbolContext(
        oi_ts=[ts - timedelta(hours=48) for ts in span] + span,
        oi_values=[D(str(1000 + i)) for i in range(len(span))]
        + [D(str(2000 + i * 2)) for i in range(len(span))],
        funding_ts=[span[0] - timedelta(days=d) for d in range(40, 0, -1)] + span,
        funding_rates=[D("0.001")] * 20
        + [D("0.02")] * 20
        + [D("0.01")] * len(span),
    )

    def signals(bars: list[MarketBar]) -> list[tuple]:
        series = build_atr_series(bars, p.stop_atr_period, H4)
        fn = make_signal_fn(wide_context, p, series)
        out = []
        for i in range(need, len(bars)):
            window = bars[: i + 1]
            signal = fn(window)
            if signal is not None:
                out.append((bars[i].ts, signal.entry, signal.stop, signal.target))
        return out

    cut = need + 30
    spoiled = list(clean)
    for i in range(cut + 1, len(spoiled)):
        wild = D(str(round(rng.uniform(1, 10_000), 6)))
        spoiled[i] = MarketBar(
            clean[i].ts, wild, wild * D("1.5"), wild * D("0.5"), wild, D("1")
        )

    horizon = clean[cut].ts
    before_clean = [s for s in signals(clean) if s[0] <= horizon]
    before_spoiled = [s for s in signals(spoiled) if s[0] <= horizon]

    assert before_clean, "no signal arose before the corruption point"
    assert before_clean == before_spoiled, "a signal changed from corrupting the future"


def test_atr_is_taken_at_the_close_of_the_entry_bar():
    """Candles are dated by the period start. Matching by OPEN time
    would take the ATR of a bar that is still in progress at the decision moment."""
    p = params()
    window = breakout_window(p)
    series = build_atr_series(window, p.stop_atr_period, H4)
    entry_bar = window[-1]

    # The value at the entry bar's close exists and differs
    # from the value at its open: the entry bar is already included in the ATR.
    assert series.at(entry_bar.ts + H4) is not None
    assert series.at(entry_bar.ts) != series.at(entry_bar.ts + H4)

    signal = signal_for(window, p)
    risk = signal.entry - signal.stop
    assert abs(risk - series.at(entry_bar.ts + H4) * p.stop_atr_mult) < D("1e-20")
