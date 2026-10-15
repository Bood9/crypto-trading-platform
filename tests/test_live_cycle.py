"""The daily cycle on the paper exchange. Phase 5, DEC-041.

The paper exchange breaks like a real one (DEC-027), so this
checks what live cycles break on: a rerun,
a position closed by a stop without our knowledge, a foreign position, a cancelled
stop, the process crashing between the write and the entry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.core.config import load_settings
from src.execution.exchange import Fill, PaperExchange
from src.execution.plan import InstrumentSpec, Order, OrderKind
from src.live.cycle import CycleContext, Sizing, realized, run_cycle
from src.live.signals import Candidate
from src.live.store import LiveTrade, MemoryStore
from src.risk.governor import GovernorLimits, TradingMode

D = Decimal
DAY = timedelta(days=1)
#: Decision at 00:05 UTC: the decision moment is midnight, the bar is the past day.
NOW = datetime(2026, 9, 21, 0, 5, tzinfo=UTC)  # Monday
MOMENT = datetime(2026, 9, 21, tzinfo=UTC)


def spec(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(symbol, D("0.01"), D("0.001"), D("0.001"), D("100000"), D("5"))


def venue(prices: dict[str, str]) -> PaperExchange:
    return PaperExchange(
        mark={s: D(p) for s, p in prices.items()},
        specs={s: spec(s) for s in prices},
    )


def cand(symbol: str, *, is_long: bool = True, close: str = "100",
         stop_pct: str = "10", moment: datetime = MOMENT) -> Candidate:
    c = D(close)
    f = D(stop_pct) / 100
    return Candidate(symbol, moment - DAY, is_long, c, c * (1 - f) if is_long else c * (1 + f))


async def uncorrelated(_a, _b, _m):
    return D("0.1")


async def no_funding(_s, _a, _b):
    return D(0)


def ctx(ex, store, candidates, *, now=NOW, dry_run=False, **kw) -> CycleContext:
    settings = load_settings()
    return CycleContext(
        setup="setup_e", venue=ex, store=store,
        limits=kw.pop("limits", GovernorLimits.from_config(settings.risk)),
        sizing=Sizing.from_config(settings.risk),
        initial_equity=D(str(settings.risk.equity_usd)),
        hold=DAY * 7, candidates=candidates, correlation=uncorrelated,
        funding_rates=kw.pop("funding", no_funding), now=now, dry_run=dry_run, **kw,
    )


def open_trades(store: MemoryStore) -> list[LiveTrade]:
    return [t for t in store.trades if t.status == "open"]


# ---------------------------------------------------------------------------
async def test_first_run_opens_protected_positions_within_the_risk_limits():
    ex, store = venue({"AUSDT": "100", "BUSDT": "50"}), MemoryStore()
    both = [cand("AUSDT"), cand("BUSDT", is_long=False, close="50")]
    report = await run_cycle(ctx(ex, store, both))

    assert not report.halted, report.notes
    assert sorted(report.opened) == ["AUSDT", "BUSDT"]
    positions = {p.symbol: p for p in await ex.positions()}
    assert positions["AUSDT"].is_long and not positions["BUSDT"].is_long
    orders = {o.link_id for o in await ex.open_orders()}
    for trade in open_trades(store):
        assert trade.stop_link_id in orders, "position without a stop"
        loss = abs(trade.entry_price - trade.stop_price) * trade.qty
        assert loss <= D("1.5"), f"risk {loss} exceeds 1.5% of $100"
        assert D("5") <= trade.entry_price * trade.qty <= D("30")
        assert trade.close_due_at == MOMENT + DAY * 7
    assert store.states["setup_e"].mode is TradingMode.ON


async def test_second_run_the_same_day_opens_nothing_twice():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    report = await run_cycle(ctx(ex, store, [cand("AUSDT")], now=NOW + timedelta(hours=2)))

    assert not report.halted, report.notes
    assert report.opened == []
    assert len(await ex.positions()) == 1
    assert len(open_trades(store)) == 1


async def test_position_closed_by_its_stop_is_booked_not_halted():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    ex.move_price("AUSDT", D("80"))  # below the stop at 90 — a gap
    assert await ex.positions() == []

    report = await run_cycle(ctx(ex, store, [], now=NOW + DAY))
    assert not report.halted, report.notes
    assert [(s, r) for s, r, _ in report.closed] == [("AUSDT", "STOP")]
    pnl = report.closed[0][2]
    assert pnl < 0
    assert store.states["setup_e"].equity == D("100") + pnl
    assert store.trades[0].status == "closed" and store.trades[0].exit_price == D("80")


async def test_time_exit_after_the_hold_frees_the_slot_for_todays_signal():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    ex.move_price("AUSDT", D("104"))

    # A day before the deadline — not closed.
    early = await run_cycle(ctx(ex, store, [], now=NOW + DAY * 6))
    assert early.closed == [] and len(await ex.positions()) == 1

    later = NOW + DAY * 7
    report = await run_cycle(ctx(ex, store, [cand("AUSDT", close="104", moment=MOMENT + DAY * 7)],
                                 now=later))
    assert not report.halted, report.notes
    assert [(s, r) for s, r, _ in report.closed] == [("AUSDT", "TIME_STOP")]
    assert report.closed[0][2] > 0
    # The same symbol is reopened on today's signal — as in the backtest.
    assert report.opened == ["AUSDT"]
    assert len(open_trades(store)) == 1
    # Old stops are cancelled; exactly one remains — the new position's.
    assert len(await ex.open_orders()) == 1


async def test_foreign_position_halts_and_the_halt_survives_restart():
    ex, store = venue({"AUSDT": "100", "ZUSDT": "10"}), MemoryStore()
    await ex.place(Order(OrderKind.ENTRY, "ZUSDT", True, D("1"), None, False, "manual-1"))

    report = await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    assert report.halted and report.opened == []
    assert store.states["setup_e"].mode is TradingMode.OFF_MANUAL

    # The position was removed — but a human re-enables trading, not the next run.
    await ex.place(Order(OrderKind.EMERGENCY_CLOSE, "ZUSDT", False, D("1"), None, True, "manual-2"))
    again = await run_cycle(ctx(ex, store, [cand("AUSDT")], now=NOW + DAY))
    assert again.opened == []
    assert store.states["setup_e"].mode is TradingMode.OFF_MANUAL


async def test_missing_stop_is_replaced_at_once_and_trading_stops():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    trade = open_trades(store)[0]
    await ex.cancel(trade.stop_link_id)  # the stop is gone

    report = await run_cycle(ctx(ex, store, [], now=NOW + DAY))
    assert report.halted
    repaired = open_trades(store)[0]
    assert repaired.stop_link_id != trade.stop_link_id
    assert repaired.stop_link_id in {o.link_id for o in await ex.open_orders()}


async def test_crash_after_write_ahead_without_entry_voids_the_record():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    await store.add_trade(LiveTrade(
        setup="setup_e", symbol="AUSDT", is_long=True, qty=D("0.1"),
        entry_price=D("100"), stop_price=D("90"), margin=D("1"),
        entry_link_id="cb-never-sent", stop_link_id="cb-stop",
        bar_ts=MOMENT - DAY, opened_at=MOMENT, close_due_at=MOMENT + DAY * 7,
    ))
    report = await run_cycle(ctx(ex, store, [], now=NOW + timedelta(hours=1)))
    assert not report.halted, report.notes
    assert store.trades[0].status == "void"


async def test_crash_after_entry_and_stop_promotes_the_record():
    """Entry and stop went through, the process crashed before recording the outcome."""
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    await ex.place(Order(OrderKind.ENTRY, "AUSDT", True, D("0.1"), None, False, "cb-e"))
    await ex.place(Order(OrderKind.STOP_LOSS, "AUSDT", False, D("0.1"), D("90"), True, "cb-s"))
    await store.add_trade(LiveTrade(
        setup="setup_e", symbol="AUSDT", is_long=True, qty=D("0.1"),
        entry_price=D("99"), stop_price=D("90"), margin=D("1"),
        entry_link_id="cb-e", stop_link_id="cb-s",
        bar_ts=MOMENT - DAY, opened_at=MOMENT, close_due_at=MOMENT + DAY * 7,
    ))
    report = await run_cycle(ctx(ex, store, [], now=NOW + timedelta(hours=1)))
    assert not report.halted, report.notes
    trade = store.trades[0]
    assert trade.status == "open" and trade.entry_price == D("100") and trade.entry_fee > 0


async def test_lost_entry_response_does_not_open_a_second_position():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    from src.execution.plan import link_id

    ex.lose_response_for.add(link_id("AUSDT", MOMENT, OrderKind.ENTRY))
    report = await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    assert not report.halted, report.notes
    assert len(await ex.positions()) == 1 and report.opened == ["AUSDT"]


async def test_price_far_from_the_signal_close_is_refused():
    ex, store = venue({"AUSDT": "120"}), MemoryStore()
    report = await run_cycle(ctx(ex, store, [cand("AUSDT", close="100")]))
    assert report.skipped == [("AUSDT", "PRICE_DIVERGENCE")]
    assert await ex.positions() == []


async def test_signal_from_an_old_bar_is_refused():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    report = await run_cycle(ctx(ex, store, [cand("AUSDT", moment=MOMENT - DAY)]))
    assert report.skipped == [("AUSDT", "STALE_SIGNAL")]


async def test_late_run_closes_but_does_not_enter():
    """A run at noon: the time exit is executed, the entry is not."""
    ex, store = venue({"AUSDT": "100", "BUSDT": "100"}), MemoryStore()
    await run_cycle(ctx(ex, store, [cand("AUSDT")]))
    later = MOMENT + DAY * 7 + timedelta(hours=12)
    report = await run_cycle(ctx(ex, store, [cand("BUSDT", moment=MOMENT + DAY * 7)], now=later))
    assert [(s, r) for s, r, _ in report.closed] == [("AUSDT", "TIME_STOP")]
    assert report.skipped == [("BUSDT", "ENTRY_WINDOW_MISSED")]
    assert await ex.positions() == []


async def test_daily_loss_limit_stops_new_entries():
    """Two stops in a row with a gap breach the 10% daily limit."""
    tight = GovernorLimits(
        max_concurrent_positions=2, max_total_margin_pct=D("35"),
        max_correlated_positions=1, correlation_threshold=D("0.7"),
        daily_loss_limit_pct=D("2"), weekly_loss_limit_pct=D("20"),
    )
    ex, store = venue({"AUSDT": "100", "BUSDT": "100", "CUSDT": "100"}), MemoryStore()
    await run_cycle(ctx(ex, store, [cand("AUSDT"), cand("BUSDT")], limits=tight))
    ex.move_price("AUSDT", D("80"))
    ex.move_price("BUSDT", D("80"))

    report = await run_cycle(ctx(ex, store, [cand("CUSDT", moment=MOMENT + DAY)],
                                 now=NOW + DAY, limits=tight))
    assert len(report.closed) == 2
    assert report.skipped == [("CUSDT", "DAILY_STOP")]
    assert await ex.positions() == []


async def test_dry_run_touches_neither_the_exchange_nor_the_store():
    ex, store = venue({"AUSDT": "100"}), MemoryStore()
    report = await run_cycle(ctx(ex, store, [cand("AUSDT")], dry_run=True))
    assert report.opened == ["AUSDT"]
    assert await ex.positions() == [] and await ex.open_orders() == []
    assert store.trades == [] and store.states == {}
    assert all(e.dry_run for e in store.entries)
    assert any(e.action == "WOULD_OPEN" for e in store.entries)


def test_realized_pnl_counts_fees_and_funding_with_sign():
    base = dict(
        setup="s", symbol="X", qty=D("2"), entry_price=D("100"), stop_price=D("90"),
        margin=D("1"), entry_link_id="e", stop_link_id="s", bar_ts=MOMENT,
        opened_at=MOMENT, close_due_at=MOMENT, entry_fee=D("0.1"),
    )
    long = LiveTrade(is_long=True, **base)
    short = LiveTrade(is_long=False, **base)
    exit_fill = Fill(avg_price=D("110"), qty=D("2"), fee=D("0.12"))
    # Positive funding: the long pays, the short receives.
    assert realized(long, exit_fill, D("0.001")) == D("20") - D("0.22") - D("0.2")
    assert realized(short, exit_fill, D("0.001")) == D("-20") - D("0.22") + D("0.2")
