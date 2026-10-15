"""Bot platform stage 0: fixes found during design. DEC-049."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.execution.plan import InstrumentSpec, Order, OrderKind
from src.live.cycle import CycleContext, Sizing, run_cycle
from src.live.lock import BookBusy, book_lock
from src.live.paper_venue import MarketPaperVenue
from src.live.signals import Candidate
from src.live.store import LiveTrade, MemoryStore, sticky_mode
from src.risk.governor import GovernorLimits, PortfolioState, TradingMode

D = Decimal
M = datetime(2026, 10, 2, tzinfo=UTC)
DAY = timedelta(days=1)


# ── book lock ───────────────────────────────────────────────
def test_second_writer_is_refused_while_the_first_holds_the_book():
    with book_lock("setup_e:paper"), pytest.raises(BookBusy), book_lock("setup_e:paper"):
        pass
    with book_lock("setup_e:paper"):  # released: can be taken again
        pass


def test_different_books_do_not_block_each_other():
    with book_lock("setup_e:paper"), book_lock("setup_e:testnet"):
        pass


async def test_run_daily_takes_the_lock_only_when_executing(monkeypatch):
    import src.live.runner as runner
    from src.core.config import load_settings

    async def fake_inner(*_a, **k):
        # The book is busy during execution: a second writer gets rejected.
        with pytest.raises(BookBusy), book_lock("setup_e:paper"):
            pass
        return "ok"

    monkeypatch.setattr(runner, "_run_daily", fake_inner)
    assert await runner.run_daily(load_settings(), "setup_e", venue_kind="paper",
                                  execute=True) == "ok"

    async def free_inner(*_a, **k):
        with book_lock("setup_e:paper"):  # preview does not hold the book
            return "ok"

    monkeypatch.setattr(runner, "_run_daily", free_inner)
    assert await runner.run_daily(load_settings(), "setup_e", venue_kind="paper",
                                  execute=False) == "ok"


# ── sticky mode and atomic enable ───────────────────────────
def test_sticky_modes_are_not_lowered_by_a_plain_save():
    assert sticky_mode(TradingMode.OFF_MANUAL, TradingMode.ON) is TradingMode.OFF_MANUAL
    assert sticky_mode(TradingMode.OFF_WEEKLY, TradingMode.OFF_DAILY) is TradingMode.OFF_WEEKLY
    assert sticky_mode(TradingMode.OFF_WEEKLY, TradingMode.OFF_MANUAL) is TradingMode.OFF_MANUAL
    assert sticky_mode(TradingMode.OFF_DAILY, TradingMode.ON) is TradingMode.ON  # clears itself
    assert sticky_mode(None, TradingMode.ON) is TradingMode.ON


async def test_a_cycle_that_started_before_a_halt_cannot_undo_it():
    store = MemoryStore()
    halted = PortfolioState.start(D(100), M)
    halted.mode = TradingMode.OFF_MANUAL
    await store.save_state("b", halted)
    stale = PortfolioState.start(D("99.5"), M)  # cycle started before the halt
    await store.save_state("b", stale)
    back = await store.load_state("b")
    assert back.mode is TradingMode.OFF_MANUAL and back.equity == D("99.5")


async def test_resume_changes_mode_and_anchors_but_not_equity():
    store = MemoryStore()
    halted = PortfolioState.start(D(100), M)
    halted.mode, halted.equity = TradingMode.OFF_WEEKLY, D(81)
    await store.save_state("b", halted)
    assert await store.resume("b", M + DAY) is TradingMode.OFF_WEEKLY
    back = await store.load_state("b")
    assert back.mode is TradingMode.ON and back.equity == D(81)
    assert back.anchors.week_equity == D(81) and back.anchors.day == (M + DAY).date()
    assert await store.resume("missing", M) is None


# ── entry retry after a failure ─────────────────────────────
def trade(link: str = "cb-e", status: str = "pending") -> LiveTrade:
    return LiveTrade(setup="b", symbol="AUSDT", is_long=True, qty=D(1), entry_price=D(100),
                     stop_price=D(90), margin=D(2), entry_link_id=link, stop_link_id="cb-s",
                     bar_ts=M - DAY, opened_at=M, close_due_at=M + DAY * 7, status=status)


async def test_voided_write_ahead_is_revived_not_duplicated():
    store = MemoryStore()
    first = await store.add_trade(trade())
    await store.update_trade(replace(first, status="void"))
    again = await store.add_trade(trade())
    assert again.id == first.id and len(store.trades) == 1 and again.status == "pending"


async def test_live_entry_key_cannot_be_reused():
    store = MemoryStore()
    await store.add_trade(trade(status="open"))
    with pytest.raises(ValueError, match="taken by trade"):
        await store.add_trade(trade())


# ── entry window by the live clock ──────────────────────────
def ctx(venue, store, clock):
    from src.core.config import load_settings

    s = load_settings()

    async def uncorrelated(*_a):
        return D("0.1")

    async def zero(*_a):
        return D(0)

    return CycleContext(
        setup="b", venue=venue, store=store, limits=GovernorLimits.from_config(s.risk),
        sizing=Sizing.from_config(s.risk), initial_equity=D(100), hold=DAY * 7,
        candidates=[Candidate("AUSDT", M - DAY, True, D(100), D(90))],
        correlation=uncorrelated, funding_rates=zero, now=M + timedelta(minutes=10),
        dry_run=False, clock=clock,
    )


def exchange():
    from src.execution.exchange import PaperExchange

    return PaperExchange(mark={"AUSDT": D(100)}, specs={
        "AUSDT": InstrumentSpec("AUSDT", D("0.01"), D("0.001"), D("0.001"), D("1e6"), D(5))})


async def test_real_venue_window_uses_the_live_clock_not_the_start_time():
    ex, store = exchange(), MemoryStore()
    report = await run_cycle(ctx(ex, store, clock=lambda: M + timedelta(hours=3)))
    assert report.skipped == [("AUSDT", "ENTRY_WINDOW_MISSED")]
    assert await ex.positions() == []


async def test_window_closing_while_the_order_is_prepared_stops_it():
    ticks = iter([M + timedelta(minutes=10), M + timedelta(hours=2, minutes=1)])
    ex, store = exchange(), MemoryStore()
    report = await run_cycle(ctx(ex, store, clock=lambda: next(ticks)))
    assert report.skipped == [("AUSDT", "ENTRY_WINDOW_MISSED")] and store.trades == []


# ── paper is saved after the order ──────────────────────────
async def test_paper_venue_is_saved_after_every_order(tmp_path):
    path = tmp_path / "v.json"
    v = MarketPaperVenue(mark={"AUSDT": D(100)})
    v.autosave = path
    await v.place(Order(OrderKind.ENTRY, "AUSDT", True, D(1), None, False, "e"))
    assert MarketPaperVenue.load(path).held_symbols == ["AUSDT"]
    await v.place(Order(OrderKind.STOP_LOSS, "AUSDT", False, D(1), D(90), True, "s"))
    await v.cancel("s")
    assert MarketPaperVenue.load(path)._resting == {}
