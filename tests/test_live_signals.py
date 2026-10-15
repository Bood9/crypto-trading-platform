"""Today's signals. Phase 5, DEC-041.

The key property is agreement with the backtest: the live cycle must
trade the rule that was tested, not a similar one. This is checked
not on a made-up example but by running the engine over the same bars.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtest.engine import BacktestConfig, MarketBar, run
from src.backtest.resample import to_daily
from src.core.config import load_settings
from src.live.signals import (
    FOUR_HOURS,
    Candidate,
    Skip,
    candidate_for,
    decision_moment,
    ordered,
)
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_d import SetupDParams, make_signal_fn, warmup_bars
from tests.test_portfolio import LIMITS, SIZING, SPEC, outcome, uncorrelated

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def params() -> SetupDParams:
    return SetupDParams.from_config(load_settings().strategy["setup_e"])


def walk(days: int, *, seed: int = 7, noise: float = 0.012) -> list[MarketBar]:
    """A random walk on 4h with alternating trends, so that
    there are signals in both directions."""
    rng = random.Random(seed)
    out, price = [], 100.0
    for i in range(days * 6):
        drift = 0.004 if (i // 120) % 2 == 0 else -0.004
        start = price
        price *= 1 + drift + rng.gauss(0, noise)
        hi, lo = max(start, price) * 1.004, min(start, price) * 0.996
        out.append(
            MarketBar(T0 + FOUR_HOURS * i, D(f"{start:.6f}"), D(f"{hi:.6f}"),
                      D(f"{lo:.6f}"), D(f"{price:.6f}"), D("1"))
        )
    return out


def engine_entries(bars_4h: list[MarketBar], p: SetupDParams) -> dict[datetime, tuple]:
    """Which entries the backtest would open, via the same path as sweep_setup_d."""
    daily = to_daily(bars_4h, FOUR_HOURS)
    atr = build_atr_series(daily, p.stop_atr_period, DAY)
    cfg = BacktestConfig(
        initial_equity=D("100"), taker_fee_pct=D("0.055"), slippage_pct=D("0.05"),
        time_stop=timedelta(hours=p.time_stop_hours), warmup_bars=warmup_bars(p),
    )
    result = run(daily, make_signal_fn(p, atr), lambda s, _p: (D("1"), s.entry, s.entry), cfg)
    return {
        o.trade.opened_at: (o.trade.is_long, o.trade.entry, o.trade.stop)
        for o in result.outcomes
    }


def test_live_candidate_equals_the_backtest_entry_on_every_entry_day():
    p = params()
    bars = walk(160)
    entries = engine_entries(bars, p)
    assert len(entries) >= 8, "too few entries: the test proves nothing"
    assert {v[0] for v in entries.values()} == {True, False}, "both sides needed"

    for opened_at, (is_long, entry, stop) in entries.items():
        # The bar is dated at the start of the day; the decision is at its close.
        got = candidate_for("SOLUSDT", bars, p, opened_at + DAY)
        assert isinstance(got, Candidate), (opened_at, got)
        assert (got.bar_ts, got.is_long, got.close, got.stop) == (
            opened_at, is_long, entry, stop,
        )


def test_future_bars_do_not_change_the_decision():
    """Bars after the decision moment are cut off here, not by the caller."""
    p = params()
    bars = walk(120)
    moment = T0 + DAY * 90
    honest = candidate_for("SOLUSDT", [b for b in bars if b.ts < moment], p, moment)
    poisoned = [
        MarketBar(b.ts, b.open, b.high * 5, b.low / 5, b.close * 3, b.volume)
        if b.ts >= moment else b
        for b in bars
    ]
    assert candidate_for("SOLUSDT", poisoned, p, moment) == honest


def test_missing_last_bar_is_stale_not_yesterdays_price():
    p = params()
    moment = T0 + DAY * 90
    bars = [b for b in walk(120) if b.ts < moment]
    assert isinstance(candidate_for("X", bars, p, moment), Candidate | Skip)
    # The day's last 4h bar is missing: no complete day for the date.
    assert candidate_for("X", bars[:-1], p, moment) is Skip.STALE_DATA
    # Collection is a whole day behind.
    assert candidate_for("X", bars[:-6], p, moment) is Skip.STALE_DATA
    assert candidate_for("X", [], p, moment) is Skip.STALE_DATA


def test_short_history_is_refused():
    p = params()
    moment = T0 + DAY * 10
    assert candidate_for("X", walk(10), p, moment) is Skip.SHORT_HISTORY


def test_decision_moment_is_last_utc_midnight():
    now = datetime(2026, 9, 25, 0, 7, tzinfo=UTC)
    assert decision_moment(now) == datetime(2026, 9, 25, tzinfo=UTC)
    assert decision_moment(now - timedelta(minutes=8)) == datetime(2026, 9, 24, tzinfo=UTC)


async def test_queue_order_matches_the_portfolio_replay():
    """When slots are short, the live cycle takes the same candidate as
    the backtest's portfolio replay. Compared against the replay ITSELF, not
    a copy of its formula: a copy would still match after the original changed."""
    from src.backtest.portfolio import replay
    from src.risk.governor import GovernorLimits

    one_slot = GovernorLimits(**{**LIMITS.__dict__, "max_concurrent_positions": 1})
    names = [f"S{i}USDT" for i in range(12)]
    seed = 20260924
    picks_live, picks_replay = [], []
    for day in range(6):
        opened = T0 + DAY * day * 10
        per_symbol = {n: [outcome(n, opened=opened, held_hours=48)] for n in names}
        book = await replay(
            per_symbol, limits=one_slot, instruments=dict.fromkeys(names, SPEC),
            initial_equity=D("100"), correlation=uncorrelated,
            tie_break_seed=seed, **SIZING,
        )
        picks_replay.append(book.taken[0].trade.symbol)
        queue = ordered([Candidate(n, opened, True, D("100"), D("97")) for n in names], seed)
        picks_live.append(queue[0].symbol)
    assert picks_live == picks_replay
    assert len(set(picks_live)) > 1, "always the same one: ordered by name?"


def test_universe_month_is_the_month_of_the_signal_bar():
    """On the first of the month the decision uses last month's bar,
    and its universe is last month's too, as in the backtest."""
    from src.live.runner import month_of_decision

    assert month_of_decision(datetime(2026, 10, 1, tzinfo=UTC)) == "2026-09"
    assert month_of_decision(datetime(2026, 10, 2, tzinfo=UTC)) == "2026-10"
