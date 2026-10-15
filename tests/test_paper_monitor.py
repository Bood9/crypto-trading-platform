"""Day watch over paper: stops immediately, no decisions.

The main invariant: the watch replays the exchange forward ONLY after
the day's decision is made (`decided_for`). Otherwise the night cycle would lose
the right to decide "as of 00:10" (six review rounds 2026-09-28).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import src.live.runner as runner
from src.backtest.engine import MarketBar
from src.core.config import load_settings
from src.execution.plan import InstrumentSpec, Order, OrderKind
from src.live.paper_venue import MarketPaperVenue
from src.live.runner import Prepared, monitor_paper
from src.live.store import LiveTrade, MemoryStore

D = Decimal
H = timedelta(hours=1)
DAY = timedelta(days=1)
M = datetime(2026, 9, 29, tzinfo=UTC)


def bar(ts, low, close):
    return MarketBar(ts, D(close), D(close), D(low), D(close), D(1))


def patch(monkeypatch, tmp_path, bars):
    async def fake_bars(symbol, _tf, start, end):
        return [b for b in bars.get(symbol, []) if start <= b.ts < end]

    async def fake_specs(names):
        return {n: InstrumentSpec(n, D("0.01"), D("0.001"), D("0.001"), D("1e6"), D(5))
                for n in names}

    async def zero(*_a):
        return D(0)

    class Uncorrelated:
        def __init__(self, *_a):
            pass

        async def between(self, *_a):
            return D("0.1")

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "instrument_specs", fake_specs)
    monkeypatch.setattr(runner, "funding_sum", zero)
    monkeypatch.setattr(runner, "CorrelationSource", Uncorrelated)
    monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)


async def seeded(tmp_path, *, decided_for) -> tuple[MarketPaperVenue, MemoryStore]:
    v = MarketPaperVenue(mark={"AUSDT": D(100)})
    await v.place(Order(OrderKind.ENTRY, "AUSDT", True, D("0.1"), None, False, "e-A"))
    await v.place(Order(OrderKind.STOP_LOSS, "AUSDT", False, D("0.1"), D(90), True, "s-A"))
    v.replayed_until = M
    v.seen_until = M
    v.decided_for = decided_for
    v.save(tmp_path / "setup_e.json")
    store = MemoryStore()
    await store.add_trade(LiveTrade(
        setup="setup_e:paper", symbol="AUSDT", is_long=True, qty=D("0.1"), entry_price=D(100),
        stop_price=D(90), margin=D(2), entry_link_id="e-A", stop_link_id="s-A",
        bar_ts=M - DAY * 2, opened_at=M - DAY, close_due_at=M + DAY * 6,
        status="open", entry_fee=D("0.005"),
    ))
    return v, store


STOP_AT_14 = {"AUSDT": [bar(M + H * i, "95" if i != 14 else "85", "96") for i in range(20)]}


async def test_before_the_nights_decision_the_monitor_does_nothing(monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path, STOP_AT_14)
    _, store = await seeded(tmp_path, decided_for=M - DAY)  # YESTERDAY's decision
    result = await monitor_paper(load_settings(), "setup_e", now=M + H * 18, store=store)
    assert result.idle and "not made yet" in result.idle
    back = MarketPaperVenue.load(tmp_path / "setup_e.json")
    assert back.replayed_until == M and back.seen_until == M, "the exchange is untouched"


async def test_after_the_decision_a_daytime_stop_is_booked_at_once(monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path, STOP_AT_14)
    _, store = await seeded(tmp_path, decided_for=M)
    now = M + H * 18 + timedelta(minutes=5)
    result = await monitor_paper(load_settings(), "setup_e", now=now, store=store)
    assert result.idle is None
    assert [(s, r) for s, r, _ in result.report.closed] == [("AUSDT", "STOP")]
    assert result.report.opened == [], "no entries during the day"
    # The bar opened above the stop and went through it: execution at the stop
    # level with slippage, not at the bar's low.
    assert store.trades[0].status == "closed" and store.trades[0].exit_price == D(90) * D("0.9995")
    back = MarketPaperVenue.load(tmp_path / "setup_e.json")
    assert back.replayed_until == M + H * 18 and back.held_symbols == []


async def test_quiet_day_changes_nothing_but_the_replay_mark(monkeypatch, tmp_path):
    patch(monkeypatch, tmp_path, {"AUSDT": [bar(M + H * i, "95", "96") for i in range(10)]})
    _, store = await seeded(tmp_path, decided_for=M)
    result = await monitor_paper(load_settings(), "setup_e", now=M + H * 10, store=store)
    assert result.report.closed == [] and result.events == []
    assert store.trades[0].status == "open"


async def test_the_night_cycle_marks_the_decision_only_when_it_decided(monkeypatch, tmp_path):
    """The `decided_for` mark is set only by a cycle that reached the decision:
    without a universe it is absent, and the day watch will not replay the exchange."""
    venue = MarketPaperVenue()
    venue.replayed_until = M

    async def fake_paper_venue(_s, _setup, _symbols, _now):
        venue.decision_passed = False
        return venue, None, []

    async def nothing(*_a):
        return {}

    async def zero(*_a):
        return D(0)

    no_universe = Prepared(M, [], [], problem="no universe")

    async def fake_prepare(*_a):
        return no_universe

    monkeypatch.setattr(runner, "paper_venue", fake_paper_venue)
    monkeypatch.setattr(runner, "prepare", fake_prepare)
    monkeypatch.setattr(runner, "load_instrument_params", nothing)
    monkeypatch.setattr(runner, "funding_sum", zero)
    await runner.run_daily(load_settings(), "setup_e", venue_kind="paper", execute=True,
                           now=M + timedelta(minutes=10), store=MemoryStore())
    assert venue.decided_for is None

    async def decided_prepare(*_a):
        return Prepared(M, ["AUSDT"], [])

    monkeypatch.setattr(runner, "prepare", decided_prepare)
    await runner.run_daily(load_settings(), "setup_e", venue_kind="paper", execute=True,
                           now=M + timedelta(minutes=10), store=MemoryStore())
    assert venue.decided_for == M


async def test_state_file_keeps_the_decision_mark(tmp_path):
    v = MarketPaperVenue()
    v.decided_for = M
    v.save(tmp_path / "s.json")
    assert MarketPaperVenue.load(tmp_path / "s.json").decided_for == M


# ── watch review 2026-09-29 ─────────────────────────────────
async def test_stop_filled_by_the_night_advance_is_booked_by_the_day(monkeypatch, tmp_path):
    """At night a late run executed the stop of the only position, but the cycle
    has not counted it yet: no positions on the exchange, the trade is open in the books.
    The watch must count it now, not the next night."""
    patch(monkeypatch, tmp_path, {})
    v, store = await seeded(tmp_path, decided_for=M)
    v.move_price("AUSDT", D(88))  # stop executed by the night run
    v.save(tmp_path / "setup_e.json")
    result = await monitor_paper(load_settings(), "setup_e", now=M + H * 4, store=store)
    assert result.idle is None, result.idle
    assert [(s, r) for s, r, _ in result.report.closed] == [("AUSDT", "STOP")]


async def test_halted_book_is_left_for_a_human(monkeypatch, tmp_path):
    from src.risk.governor import PortfolioState, TradingMode

    patch(monkeypatch, tmp_path, STOP_AT_14)
    _, store = await seeded(tmp_path, decided_for=M)
    state = PortfolioState.start(D(100), M)
    state.mode = TradingMode.OFF_MANUAL
    await store.save_state("setup_e:paper", state)
    result = await monitor_paper(load_settings(), "setup_e", now=M + H * 18, store=store)
    assert result.idle and "waiting for a human" in result.idle
    assert MarketPaperVenue.load(tmp_path / "setup_e.json").replayed_until == M


async def test_a_halted_night_does_not_mark_the_decision(monkeypatch, tmp_path):
    """A cycle halted by reconciliation did not reach the decision."""
    from src.live.cycle import CycleReport
    from src.risk.governor import TradingMode

    venue = MarketPaperVenue()
    venue.replayed_until = M

    async def fake_paper_venue(*_a):
        venue.decision_passed = False
        return venue, None, []

    async def fake_prepare(*_a):
        return Prepared(M, ["AUSDT"], [])

    async def halted_cycle(_ctx):
        rep = CycleReport(moment=M, dry_run=False, halted=True)
        rep.equity, rep.mode = D(100), TradingMode.OFF_MANUAL
        return rep

    async def nothing(*_a):
        return {}

    monkeypatch.setattr(runner, "paper_venue", fake_paper_venue)
    monkeypatch.setattr(runner, "prepare", fake_prepare)
    monkeypatch.setattr(runner, "run_cycle", halted_cycle)
    monkeypatch.setattr(runner, "load_instrument_params", nothing)
    await runner.run_daily(load_settings(), "setup_e", venue_kind="paper", execute=True,
                           now=M + timedelta(minutes=5), store=MemoryStore())
    assert venue.decided_for is None


async def test_mode_off_without_candidates_writes_no_hourly_journal_row(monkeypatch, tmp_path):
    from src.risk.governor import PortfolioState, TradingMode

    patch(monkeypatch, tmp_path, {"AUSDT": [bar(M + H * i, "95", "96") for i in range(5)]})
    _, store = await seeded(tmp_path, decided_for=M)
    state = PortfolioState.start(D(100), M)
    state.mode = TradingMode.OFF_DAILY  # daily limit: the watch runs, but silently
    await store.save_state("setup_e:paper", state)
    await monitor_paper(load_settings(), "setup_e", now=M + H * 5, store=store)
    assert not [e for e in store.entries if e.action == "NO_ENTRIES"]
