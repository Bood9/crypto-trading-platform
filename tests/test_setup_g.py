"""Setup G: rank momentum. DEC-044.

Checked against the preregistration, population neutrality
(equal longs and shorts), and that the rank at the close sees the future
of NOT A SINGLE symbol.
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
from src.strategy.setup_g import RankBook, SetupGParams, make_signal_fn, warmup_bars

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2023, 1, 1, tzinfo=UTC)


def params(**over) -> SetupGParams:
    return replace(SetupGParams.from_config(load_settings().strategy["setup_g"]), **over)


def walk(n: int, drift: float, seed: int) -> list[MarketBar]:
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(n):
        start = price
        price *= 1 + drift + rng.gauss(0, 0.02)
        out.append(MarketBar(T0 + DAY * i, D(f"{start:.4f}"), D(f"{max(start, price) * 1.01:.4f}"),
                             D(f"{min(start, price) * 0.99:.4f}"), D(f"{price:.4f}"), D("1")))
    return out


def universe(n_symbols: int = 12, days: int = 200) -> dict[str, list[MarketBar]]:
    return {
        f"S{i:02d}USDT": walk(days, (i - n_symbols / 2) * 0.001, seed=i)
        for i in range(n_symbols)
    }


def everyone(_symbol, _ts) -> bool:
    return True


def test_config_matches_the_preregistration():
    p = params()
    assert (p.lookback_days, p.top_k, p.min_candidates, p.hold_days) == (28, 3, 10, 7)
    assert (p.stop_atr_mult, p.stop_atr_period, p.max_stop_pct) == (D(2), 14, D(30))


def test_min_candidates_must_separate_longs_from_shorts():
    cfg = dict(load_settings().strategy["setup_g"])
    cfg["min_candidates"] = 6  # 2 × max(K) = 10
    with pytest.raises(ValueError, match="would overlap"):
        SetupGParams.from_config(cfg)


def test_g_is_frozen_with_its_runner_and_the_return_function_of_d():
    from src.backtest.validation import frozen_code

    plan = load_settings().backtest.validation["setup_g"]
    code = frozen_code("setup_g", tuple(plan.extra_code))
    for path in ("src/strategy/setup_g.py", "src/backtest/runner_g.py", "src/strategy/setup_d.py"):
        assert path in code


def test_strongest_go_long_weakest_go_short_equal_counts():
    daily = universe()
    book = RankBook(daily, params(), everyone)
    close = T0 + DAY * 150
    sides = {s: book.direction(s, close) for s in daily}
    longs = sorted(s for s, d in sides.items() if d == 1)
    shorts = sorted(s for s, d in sides.items() if d == -1)
    assert len(longs) == len(shorts) == 3

    def ret(s):
        bars = daily[s]
        return bars[149].close / bars[149 - 28].close
    ranked = sorted(daily, key=ret)
    assert set(shorts) == set(ranked[:3]) and set(longs) == set(ranked[-3:])


def test_too_few_candidates_means_no_ranking():
    daily = dict(list(universe().items())[:9])
    book = RankBook(daily, params(), everyone)
    assert not book.ranked(T0 + DAY * 150)


def test_only_members_of_the_month_are_ranked():
    daily = universe()
    outsider = "S11USDT"  # the strongest by construction
    book = RankBook(daily, params(), lambda s, _ts: s != outsider)
    close = T0 + DAY * 150
    assert book.direction(outsider, close) is None
    assert sum(1 for s in daily if book.direction(s, close) == 1) == 3


def test_the_future_of_any_symbol_cannot_change_a_past_rank():
    daily = universe()
    honest = RankBook(daily, params(), everyone)
    cut = T0 + DAY * 120
    poisoned = {
        s: [
            MarketBar(b.ts, b.open, b.high * 9, b.low,
                      b.close * (9 if i % 2 else D("0.1")), b.volume)
            if b.ts >= cut else b
            for b in bars
        ]
        for i, (s, bars) in enumerate(daily.items())
    }
    bad = RankBook(poisoned, params(), everyone)
    for day in range(30, 120):
        close = T0 + DAY * day
        for s in daily:
            assert honest.direction(s, close) == bad.direction(s, close), (close, s)


def test_ties_never_put_one_symbol_on_both_sides():
    """Identical series: longs and shorts must not overlap,
    and the choice must depend only on the name."""
    flat = {f"S{i:02d}USDT": walk(100, 0, seed=0) for i in range(12)}
    book = RankBook(flat, params(), everyone)
    close = T0 + DAY * 80
    longs = sorted(s for s in flat if book.direction(s, close) == 1)
    shorts = sorted(s for s in flat if book.direction(s, close) == -1)
    assert longs == ["S00USDT", "S01USDT", "S02USDT"]
    assert shorts == ["S09USDT", "S10USDT", "S11USDT"]


def engine_sides(daily, p, seed=None):
    book = RankBook(daily, p, everyone)
    sides = []
    for symbol, bars in daily.items():
        atr = build_atr_series(bars, p.stop_atr_period, DAY)
        flip = None if seed is None else f"{seed}:{symbol}"
        fn = make_signal_fn(p, symbol, atr, book, coin_flip_seed=flip)
        cfg = BacktestConfig(
            initial_equity=D(100), taker_fee_pct=D("0.055"), slippage_pct=D("0.05"),
            time_stop=timedelta(hours=p.time_stop_hours), warmup_bars=warmup_bars(p),
        )
        res = run(bars, fn, lambda s, _p: (D(1), s.entry, s.entry), cfg)
        sides += [(symbol, o.trade.opened_at, o.trade.is_long) for o in res.outcomes]
    return sides


def test_engine_population_is_balanced_between_longs_and_shorts():
    trades = engine_sides(universe(days=260), params(max_stop_pct=D(100)))
    longs = sum(1 for *_, is_long in trades if is_long)
    shorts = len(trades) - longs
    assert longs > 10 and shorts > 10
    assert abs(longs - shorts) / len(trades) < 0.2


def test_coin_flip_keeps_the_first_entry_and_changes_sides():
    daily = universe(days=260)
    p = params(max_stop_pct=D(100))
    real = engine_sides(daily, p)
    flipped = engine_sides(daily, p, seed=1)
    first = lambda xs: sorted((s, t) for s, t, _ in xs)[0]  # noqa: E731
    assert first(real) == first(flipped)
    assert [x[2] for x in sorted(real)] != [x[2] for x in sorted(flipped)]
