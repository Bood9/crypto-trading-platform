"""Preparing a live decision: history for ATR and an empty universe.

Both tests come from findings of the 2026-09-28 night recheck:
* the live ATR was computed over 39 days and diverged from the backtest, while the
  equivalence test fed the live code the full history and did not see it;
* without the month's universe the cycle failed entirely — exits included.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import src.live.runner as runner
from src.backtest.engine import MarketBar
from src.core.config import load_settings
from src.execution.plan import Order, OrderKind
from src.live.paper_venue import MarketPaperVenue
from src.live.signals import FOUR_HOURS, Candidate, candidate_for
from src.live.store import LiveTrade, MemoryStore
from src.strategy.setup_d import SetupDParams

D = Decimal
DAY = timedelta(days=1)
MOMENT = datetime(2026, 9, 28, tzinfo=UTC)


def history(days: int, seed: int = 11) -> list[MarketBar]:
    """4h bars with changing volatility: ATR then remembers where the count started."""
    rng = random.Random(seed)
    start = MOMENT - DAY * days
    out, price = [], 100.0
    for i in range(days * 6):
        vol = 0.004 if (i // 300) % 2 else 0.02
        o = price
        price *= 1 + 0.0015 + rng.gauss(0, vol)
        out.append(MarketBar(start + FOUR_HOURS * i, D(f"{o:.6f}"),
                             D(f"{max(o, price) * (1 + vol):.6f}"),
                             D(f"{min(o, price) * (1 - vol):.6f}"), D(f"{price:.6f}"), D(1)))
    return out


def params() -> SetupDParams:
    return SetupDParams.from_config(load_settings().strategy["setup_e"])


async def test_live_stop_matches_the_full_history_stop(monkeypatch):
    bars = history(900)

    async def fake_universe(_month):
        return ["AUSDT"]

    async def fake_bars(_symbol, _tf, start, end):
        return [b for b in bars if start <= b.ts < end]

    monkeypatch.setattr(runner, "universe_of", fake_universe)
    monkeypatch.setattr(runner, "load_bars", fake_bars)

    prepared = await runner.prepare(load_settings(), "setup_e", MOMENT + timedelta(minutes=10))
    full = candidate_for("AUSDT", bars, params(), MOMENT)
    assert isinstance(full, Candidate), "a test without a signal checks nothing"
    assert prepared.candidates, "the live code produced no signal"
    live = prepared.candidates[0]
    assert abs(live.stop - full.stop) / full.close < D("1e-9")

    # The test distinguishes windows: over 39 days the stop is noticeably different.
    recent = [b for b in bars if b.ts >= MOMENT - DAY * 39]
    short = candidate_for("AUSDT", recent, params(), MOMENT)
    assert abs(short.stop - full.stop) / full.close > D("1e-4")


async def test_missing_universe_is_a_problem_not_a_crash(monkeypatch):
    async def empty(_month):
        return []

    monkeypatch.setattr(runner, "universe_of", empty)
    prepared = await runner.prepare(load_settings(), "setup_e", MOMENT + timedelta(minutes=10))
    assert prepared.candidates == [] and prepared.universe == []
    assert "no universe for 2026-09" in prepared.problem


async def test_without_universe_due_positions_still_close(monkeypatch):
    """There is no universe, but positions are due for a time exit — they are closed."""
    async def empty(_month):
        return []

    venue = MarketPaperVenue(mark={"AUSDT": D(110)})
    await venue.place(Order(OrderKind.ENTRY, "AUSDT", True, D("0.1"), None, False, "cb-e"))
    await venue.place(Order(OrderKind.STOP_LOSS, "AUSDT", False, D("0.1"), D(90), True, "cb-s"))
    venue.mark["AUSDT"] = D(112)

    async def fake_paper_venue(_settings, _setup, symbols, _now):
        assert symbols == []
        return venue, None, []

    async def no_params(_symbols):
        return {}

    async def no_funding(_s, _a, _b):
        return D(0)

    store = MemoryStore()
    await store.add_trade(LiveTrade(
        setup="setup_e:paper", symbol="AUSDT", is_long=True, qty=D("0.1"), entry_price=D(110),
        stop_price=D(90), margin=D(2), entry_link_id="cb-e", stop_link_id="cb-s",
        bar_ts=MOMENT - DAY * 8, opened_at=MOMENT - DAY * 7, close_due_at=MOMENT,
        status="open", entry_fee=D("0.006"),
    ))
    monkeypatch.setattr(runner, "universe_of", empty)
    monkeypatch.setattr(runner, "paper_venue", fake_paper_venue)
    monkeypatch.setattr(runner, "load_instrument_params", no_params)
    monkeypatch.setattr(runner, "funding_sum", no_funding)

    prepared, report, _ = await runner.run_daily(
        load_settings(), "setup_e", venue_kind="paper", execute=True,
        now=MOMENT + timedelta(minutes=10), store=store,
    )
    assert prepared.problem
    assert [(s, r) for s, r, _ in report.closed] == [("AUSDT", "TIME_STOP")]
    assert await venue.positions() == []
