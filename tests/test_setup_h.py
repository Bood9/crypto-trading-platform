"""Setup H: reversion after an extreme day. DEC-046."""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtest.engine import BacktestConfig, MarketBar, PastView, run
from src.core.config import load_settings
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_h import SetupHParams, make_signal_fn, warmup_bars

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2023, 1, 1, tzinfo=UTC)


def params(**over) -> SetupHParams:
    return replace(SetupHParams.from_config(load_settings().strategy["setup_h"]), **over)


def calm(n: int, price: float = 100.0) -> list[MarketBar]:
    """Calm days: price is flat, range 2."""
    return [MarketBar(T0 + DAY * i, D(str(price)), D(str(price + 1)), D(str(price - 1)),
                      D(str(price)), D(1)) for i in range(n)]


def with_day(bars: list[MarketBar], move: float, *, gap: bool = False) -> list[MarketBar]:
    last = bars[-1]
    ts = last.ts + DAY * (2 if gap else 1)
    close = last.close + D(str(move))
    hi, lo = max(last.close, close) + 1, min(last.close, close) - 1
    return [*bars, MarketBar(ts, last.close, hi, lo, close, D(1))]


def decide(bars: list[MarketBar], p: SetupHParams | None = None, seed: str | None = None):
    p = p or params()
    atr = build_atr_series(bars, p.stop_atr_period, DAY)
    return make_signal_fn(p, atr, coin_flip_seed=seed)(PastView(bars, len(bars)))


def test_config_matches_the_preregistration():
    p = params()
    assert (p.extreme_atr_mult, p.hold_days) == (D(3), 3)
    assert (p.stop_atr_mult, p.stop_atr_period, p.max_stop_pct) == (D(2), 14, D(30))


def test_extreme_up_day_is_sold_and_extreme_down_day_is_bought():
    base = calm(30)
    up, down = decide(with_day(base, 7)), decide(with_day(base, -7))
    assert up is not None and not up.is_long
    assert down is not None and down.is_long


def test_ordinary_day_gives_nothing():
    assert decide(with_day(calm(30), 5)) is None  # 2.5 × ATR(=2) < 3


def test_the_day_is_measured_by_yesterdays_atr_not_its_own():
    """A move of 7 with yesterday's ATR at 2 is 3.5 units. The day itself inflates
    the ATR above 7/3, and by today's ATR there would be no extreme."""
    bars = with_day(calm(30), 7)
    p = params()
    atr = build_atr_series(bars, p.stop_atr_period, DAY)
    today = atr.at(bars[-1].ts + DAY)
    assert today * p.extreme_atr_mult > 7, "the test does not tell the measures apart"
    assert decide(bars) is not None


def test_a_missing_day_is_not_a_one_day_move():
    assert decide(with_day(calm(30), -7, gap=True)) is None


def test_stop_uses_the_atr_including_the_day():
    sig = decide(with_day(calm(30), -7))
    p = params()
    bars = with_day(calm(30), -7)
    atr = build_atr_series(bars, p.stop_atr_period, DAY).at(bars[-1].ts + DAY)
    assert sig.entry - sig.stop == atr * p.stop_atr_mult


def walk(n: int, seed: int = 4) -> list[MarketBar]:
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        start = price
        shock = rng.choice([0, 0, 0, 0, 0, 0, 0, 0, 0.18, -0.18])
        price *= 1 + rng.gauss(0, 0.015) + shock
        out.append(MarketBar(T0 + DAY * i, D(f"{start:.4f}"), D(f"{max(start, price) * 1.005:.4f}"),
                             D(f"{min(start, price) * 0.995:.4f}"), D(f"{price:.4f}"), D(1)))
    return out


def engine(bars, p, seed=None):
    atr = build_atr_series(bars, p.stop_atr_period, DAY)
    fn = make_signal_fn(p, atr, coin_flip_seed=seed)
    cfg = BacktestConfig(
        initial_equity=D(100), taker_fee_pct=D("0.055"), slippage_pct=D("0.05"),
        time_stop=timedelta(hours=p.time_stop_hours), warmup_bars=warmup_bars(p),
    )
    res = run(bars, fn, lambda s, _p: (D(1), s.entry, s.entry), cfg)
    return [(o.trade.opened_at, o.trade.is_long) for o in res.outcomes]


def test_engine_enters_against_the_day_in_both_directions():
    bars = walk(400)
    trades = engine(bars, params(max_stop_pct=D(100)))
    by_ts = {b.ts: i for i, b in enumerate(bars)}
    assert len(trades) >= 10
    assert {s for _, s in trades} == {True, False}
    for opened, is_long in trades:
        i = by_ts[opened]
        assert (bars[i].close < bars[i - 1].close) == is_long


def test_later_bars_cannot_change_an_earlier_entry():
    bars = walk(400)
    p = params(max_stop_pct=D(100))
    honest = engine(bars[:300], p)
    poisoned = bars[:300] + [MarketBar(b.ts, b.open, b.high * 5, b.low / 5, b.close * 3, b.volume)
                             for b in bars[300:]]
    later = engine(poisoned, p)
    assert [t for t in later if t[0] < bars[299].ts] == [t for t in honest if t[0] < bars[299].ts]


def test_coin_flip_keeps_the_first_entry_and_changes_sides():
    bars = walk(400)
    p = params(max_stop_pct=D(100))
    real, flipped = engine(bars, p), engine(bars, p, seed="1:X")
    assert real[0][0] == flipped[0][0]
    assert [s for _, s in real] != [s for _, s in flipped]
