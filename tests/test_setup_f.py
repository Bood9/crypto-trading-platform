"""Setup F: funding skew, against the crowd. DEC-042.

Checked against the preregistration, and that the future is unavailable
by construction: the skew measure and its percentile at the close do not change
if everything that happened later is corrupted.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import BacktestConfig, MarketBar, run
from src.core.config import load_settings
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_f import (
    FundingTape,
    PressureSeries,
    SetupFParams,
    make_signal_fn,
    warmup_bars,
)

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2023, 1, 1, tzinfo=UTC)


def params(**over) -> SetupFParams:
    return replace(SetupFParams.from_config(load_settings().strategy["setup_f"]), **over)


def tape_8h(daily_rates: list[float], start: datetime = T0) -> FundingTape:
    """Three accruals per day at 8 hours; the day's rate is one for all three."""
    ts, rates = [], []
    for day, rate in enumerate(daily_rates):
        for k in range(3):
            ts.append(start + DAY * day + timedelta(hours=8 * (k + 1)))
            rates.append(D(str(rate)))
    return FundingTape(ts=ts, rates=rates, interval_minutes=[480] * len(ts))


def closes(n: int) -> list[datetime]:
    return [T0 + DAY * (i + 1) for i in range(n)]


# ── config ──────────────────────────────────────────────────
def test_config_matches_the_preregistration():
    p = params()
    assert (p.funding_window_days, p.history_days, p.min_history) == (3, 180, 120)
    assert p.extreme_pct == D(10) and p.base_rate_8h == D("0.0001")
    assert (p.hold_days, p.stop_atr_mult, p.stop_atr_period, p.max_stop_pct) == (3, D(2), 14, D(30))


def test_every_knob_has_a_sweep_containing_the_base():
    cfg = dict(load_settings().strategy["setup_f"])
    cfg["hold_days"] = 5
    with pytest.raises(ValueError, match="from its own sweep"):
        SetupFParams.from_config(cfg)


def test_f_is_frozen_with_its_own_runner():
    from src.backtest.validation import frozen_code

    plan = load_settings().backtest.validation["setup_f"]
    code = frozen_code("setup_f", tuple(plan.extra_code))
    assert "src/strategy/setup_f.py" in code and "src/backtest/runner_f.py" in code


# ── skew measure ────────────────────────────────────────────
def test_pressure_is_the_average_cost_per_8h():
    tape = tape_8h([0.0003] * 5)
    assert tape.pressure(T0 + DAY * 5, DAY * 3, D("0.9")) == D("0.0003")


def test_hourly_prints_are_not_overweighted():
    """Three days: one day of 8-hour accruals at 0.0008, two days of hourly
    at 0.0001 (= 0.0008 per 8 h). The average payment per 8 h is 0.0008. The mean
    over rate_8h records would give the same, while the mean over raw records without
    normalization would be eight times smaller."""
    ts, rates, ivl = [], [], []
    for k in range(3):
        ts.append(T0 + timedelta(hours=8 * (k + 1)))
        rates.append(D("0.0008"))
        ivl.append(480)
    for h in range(48):
        ts.append(T0 + DAY + timedelta(hours=h + 1))
        rates.append(D("0.0001"))
        ivl.append(60)
    tape = FundingTape(ts, rates, ivl)
    assert tape.pressure(T0 + DAY * 3, DAY * 3, D("0.9")) == D("0.0008")


def test_uncovered_window_is_unknown_not_zero():
    tape = tape_8h([0.0003] * 5)
    holey = FundingTape(tape.ts[:9] + tape.ts[12:], tape.rates[:9] + tape.rates[12:],
                        tape.interval_minutes[:9] + tape.interval_minutes[12:])
    assert holey.pressure(T0 + DAY * 5, DAY * 3, D("0.9")) is None
    assert tape.pressure(T0 + DAY * 5, DAY * 3, D("0.9")) is not None


def test_prints_after_the_close_are_invisible():
    tape = tape_8h([0.0001] * 5 + [0.05] * 5)
    assert tape.pressure(T0 + DAY * 5, DAY * 3, D("0.9")) == D("0.0001")


# ── extreme and direction ───────────────────────────────────
def history(n: int = 200, seed: int = 1) -> list[float]:
    rng = random.Random(seed)
    return [0.0001 + rng.gauss(0, 0.00005) for _ in range(n)]


def test_crowded_longs_give_a_short_and_crowded_shorts_a_long():
    p = params(funding_window_days=1)
    base = history()
    hot = PressureSeries(tape_8h(base + [0.001]), closes(len(base) + 1), p)
    cold = PressureSeries(tape_8h(base + [-0.001]), closes(len(base) + 1), p)
    last = closes(len(base) + 1)[-1]
    assert hot.direction(last) == -1
    assert cold.direction(last) == 1


def test_record_low_that_is_still_positive_is_not_crowded_shorts():
    """The lower percentile with a positive payment is not a skew."""
    p = params(funding_window_days=1)
    base = [0.0003 + x for x in history()]
    s = PressureSeries(tape_8h(base + [0.00005]), closes(len(base) + 1), p)
    assert s.direction(closes(len(base) + 1)[-1]) is None


def test_record_high_below_the_base_rate_is_not_crowded_longs():
    p = params(funding_window_days=1)
    base = [x - 0.0001 for x in history()]  # around zero
    s = PressureSeries(tape_8h(base + [0.00009]), closes(len(base) + 1), p)
    assert s.direction(closes(len(base) + 1)[-1]) is None


def test_short_history_gives_no_signal():
    p = params(funding_window_days=1)
    base = history(100)
    s = PressureSeries(tape_8h(base + [0.001]), closes(len(base) + 1), p)
    assert s.direction(closes(len(base) + 1)[-1]) is None


def test_later_funding_cannot_change_an_earlier_decision():
    p = params(funding_window_days=3)
    base = history(260, seed=5)
    n = len(base)
    honest = PressureSeries(tape_8h(base), closes(n), p)
    poisoned_rates = base[:200] + [0.01 if i % 2 else -0.01 for i in range(60)]
    poisoned = PressureSeries(tape_8h(poisoned_rates), closes(n), p)
    for c in closes(n)[:200]:
        assert honest.direction(c) == poisoned.direction(c)
        assert honest.value(c) == poisoned.value(c)


# ── signal and engine ───────────────────────────────────────
def bars(n: int, seed: int = 3) -> list[MarketBar]:
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        start = price
        price *= 1 + rng.gauss(0, 0.02)
        out.append(MarketBar(T0 + DAY * i, D(f"{start:.4f}"), D(f"{max(start, price) * 1.01:.4f}"),
                             D(f"{min(start, price) * 0.99:.4f}"), D(f"{price:.4f}"), D("1")))
    return out


def spiky_funding(n: int, seed: int = 9) -> list[float]:
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        r = 0.0001 + rng.gauss(0, 0.00005)
        roll = rng.random()
        if roll < 0.04:
            r = 0.002
        elif roll < 0.08:
            r = -0.002
        out.append(r)
    return out


def engine_trades(bs, funding, p, seed=None):
    atr = build_atr_series(bs, p.stop_atr_period, DAY)
    pressure = PressureSeries(tape_8h(funding), [b.ts + DAY for b in bs], p)
    fn = make_signal_fn(p, atr, pressure, coin_flip_seed=seed)
    cfg = BacktestConfig(initial_equity=D(100), taker_fee_pct=D("0.055"), slippage_pct=D("0.05"),
                         time_stop=timedelta(hours=p.time_stop_hours), warmup_bars=warmup_bars(p))
    res = run(bs, fn, lambda s, _p: (D(1), s.entry, s.entry), cfg)
    return [(o.trade.opened_at, o.trade.is_long) for o in res.outcomes]


def test_engine_trades_against_the_crowd_in_both_directions():
    p = params(funding_window_days=1)
    bs, funding = bars(420), spiky_funding(420)
    trades = engine_trades(bs, funding, p)
    assert len(trades) >= 10
    assert {side for _, side in trades} == {True, False}
    for opened, is_long in trades:
        rate = funding[(opened - T0).days]
        assert (rate < 0) == is_long, (opened, rate, is_long)


def test_coin_flip_keeps_the_days_and_changes_only_sides():
    p = params(funding_window_days=1)
    bs, funding = bars(420), spiky_funding(420)
    real = engine_trades(bs, funding, p)
    flipped = engine_trades(bs, funding, p, seed="1:X")
    # The first entry is on the same day: the trigger does not depend on direction.
    # Later the days may diverge: for the other side the stop fires differently.
    assert real[0][0] == flipped[0][0]
    assert [s for _, s in real] != [s for _, s in flipped]
