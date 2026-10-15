"""Retry of the paper cycle on the same day (fourth review round 2026-09-28).

Invariant: deciding as of moment M is allowed only if the paper exchange has not yet
been replayed past M. An attempt without a decision does not replay the exchange forward;
an attempt after a replay does not enter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import src.live.runner as runner
from src.backtest.engine import MarketBar
from src.core.config import load_settings
from src.execution.plan import InstrumentSpec, Order, OrderKind
from src.live.paper_venue import MarketPaperVenue
from src.live.runner import Prepared
from src.live.signals import Candidate
from src.live.store import LiveTrade, MemoryStore

D = Decimal
H = timedelta(hours=1)
DAY = timedelta(days=1)
M = datetime(2026, 10, 2, tzinfo=UTC)


def bar(ts, low, close):
    return MarketBar(ts, D(close), D(close), D(low), D(close), D(1))


async def held(stop: str = "90") -> MarketPaperVenue:
    v = MarketPaperVenue(mark={"AUSDT": D(100)}, specs={
        s: InstrumentSpec(s, D("0.01"), D("0.001"), D("0.001"), D("1e6"), D(5))
        for s in ("AUSDT", "BUSDT")
    })
    await v.place(Order(OrderKind.ENTRY, "AUSDT", True, D("0.1"), None, False, "e-A"))
    await v.place(Order(OrderKind.STOP_LOSS, "AUSDT", False, D("0.1"), D(stop), True, "s-A"))
    return v


def trade() -> LiveTrade:
    return LiveTrade(
        setup="setup_e:paper", symbol="AUSDT", is_long=True, qty=D("0.1"), entry_price=D(100),
        stop_price=D(90), margin=D(2), entry_link_id="e-A", stop_link_id="s-A",
        bar_ts=M - DAY * 3, opened_at=M - DAY * 2, close_due_at=M + DAY * 5,
        status="open", entry_fee=D("0.005"),
    )


def patch(monkeypatch, venue, prepared, bars):
    async def fake_prepare(_s, _setup, _now):
        return prepared

    async def fake_paper_venue(_s, _setup, _symbols, _now):
        # Like the real one: has the exchange already been replayed past the decision moment?
        venue.seen_until = max(venue.replayed_until, venue.seen_until or venue.replayed_until)
        venue.decision_passed = venue.seen_until > M
        venue.replayed_until = max(venue.replayed_until, M)
        return venue, None, []

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in bars.get(symbol, []) if start <= b.ts < end]

    async def nothing(*_a):
        return {}

    async def zero(*_a):
        return D(0)

    class Uncorrelated:
        def __init__(self, *_a):
            pass

        async def between(self, *_a):
            return D("0.1")

    monkeypatch.setattr(runner, "CorrelationSource", Uncorrelated)
    monkeypatch.setattr(runner, "prepare", fake_prepare)
    monkeypatch.setattr(runner, "paper_venue", fake_paper_venue)
    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "load_instrument_params", nothing)
    monkeypatch.setattr(runner, "funding_sum", zero)


async def run(monkeypatch, venue, prepared, now, bars, store):
    patch(monkeypatch, venue, prepared, bars)
    return await runner.run_daily(load_settings(), "setup_e", venue_kind="paper",
                                  execute=True, now=now, store=store)


async def test_attempt_without_a_decision_does_not_move_the_venue(monkeypatch):
    """No universe at 02:10: the 01:00 stop is NOT replayed forward by this
    attempt; the next one decides on the state as of the decision moment."""
    venue = await held()
    venue.replayed_until = M
    bars = {"AUSDT": [bar(M, "99", "99"), bar(M + H, "85", "86")]}
    store = MemoryStore()
    await store.add_trade(trade())
    no_universe = Prepared(M, [], [], problem="no universe for 2026-10")
    await run(monkeypatch, venue, no_universe, M + H * 2 + timedelta(minutes=10), bars, store)
    assert venue.replayed_until == M
    assert [p.symbol for p in await venue.positions()] == ["AUSDT"]


async def test_retry_after_the_venue_moved_past_the_decision_does_not_enter(monkeypatch):
    """The exchange is replayed to 02:00 (the AUSDT stop fired at 01:00). The retry
    decides "as of 00:10" and does not enter: the slot was freed by a stop AFTER 00:10."""
    venue = await held()
    venue.replayed_until = M + H * 2
    venue.move_price("AUSDT", D(86))  # stop already executed by the previous attempt
    venue.mark["BUSDT"] = D(50)
    store = MemoryStore()
    await store.add_trade(trade())
    cand = Candidate("BUSDT", M - DAY, True, D(50), D(45))
    prepared = Prepared(M, ["BUSDT"], [cand])
    _, report, _ = await run(monkeypatch, venue, prepared, M + H * 3, {}, store)
    assert report.opened == []
    assert prepared.note and "this day's entries are not repeated" in prepared.note
    # The stop is still counted: an exit is not a decision and is not postponed.
    assert [(s, r) for s, r, _ in report.closed] == [("AUSDT", "STOP")]


async def test_retry_on_an_undecided_day_enters_on_honest_state(monkeypatch):
    """Attempt 1 without a universe did not replay the exchange; attempt 2 decides as of M:
    AUSDT still holds (a 01:00 stop is the future for 00:10), one of two slots
    is taken, BUSDT enters at price M, then a forward replay."""
    venue = await held()
    venue.replayed_until = M
    venue.mark["BUSDT"] = D(50)
    bars = {"AUSDT": [bar(M, "99", "99"), bar(M + H, "85", "86")],
            "BUSDT": [bar(M, "49", "50"), bar(M + H, "49", "50")]}
    store = MemoryStore()
    await store.add_trade(trade())
    cand = Candidate("BUSDT", M - DAY, True, D(50), D(45))
    _, report, events = await run(monkeypatch, venue, Prepared(M, ["BUSDT"], [cand]),
                                  M + H * 2 + timedelta(minutes=10), bars, store)
    assert report.opened == ["BUSDT"], report.skipped
    assert report.closed == [], "a stop after 00:10 does not affect the 00:10 decision"
    assert venue.replayed_until == M + H * 2 and any("AUSDT" in e for e in events)


async def test_partial_advance_still_marks_the_decision_as_passed(monkeypatch, tmp_path):
    """Fifth review round: BUSDT has no bars after M, AUSDT has a stop at 01:00.
    "Replayed until" across all is M (gap in BUSDT), but the exchange SAW 01:00:
    a same-day retry may no longer decide "as of 00:10"."""
    from src.live.runner import advance, paper_venue

    bars = {
        "AUSDT": [bar(M - H, "99", "100"), bar(M, "99", "99"), bar(M + H, "85", "86")],
        "BUSDT": [bar(M - H, "49", "50")],
    }

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in bars.get(symbol, []) if start <= b.ts < end]

    async def fake_specs(names):
        return {}

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "instrument_specs", fake_specs)
    monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)

    venue = await held()
    venue.mark["BUSDT"] = D(50)
    await venue.place(Order(OrderKind.ENTRY, "BUSDT", True, D(1), None, False, "e-B"))
    await venue.place(Order(OrderKind.STOP_LOSS, "BUSDT", False, D(1), D(40), True, "s-B"))
    venue.replayed_until = M
    await advance(venue, M + H * 3)
    assert venue.replayed_until == M, "the BUSDT gap holds the stop check in place"
    assert [p.symbol for p in await venue.positions()] == ["BUSDT"], "AUSDT stop executed"
    venue.save(tmp_path / "setup_e.json")

    again, _, _ = await paper_venue(load_settings(), "setup_e", [], M + timedelta(minutes=10))
    assert again.decision_passed


async def test_old_state_file_without_seen_until_is_read(tmp_path):
    """State file from past versions: "seen" = "replayed until"."""
    import json

    venue = await held()
    venue.replayed_until = M + H
    raw = venue.dump()
    raw.pop("seen_until")
    (tmp_path / "s.json").write_text(json.dumps(raw))
    back = MarketPaperVenue.load(tmp_path / "s.json")
    assert back.seen_until == M + H
