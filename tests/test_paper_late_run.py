"""Late paper run: execution at the decision moment. DEC-041.

The Mac may sleep through the entry window. Paper executes the decision at the
decision-moment price and replays stops over the bars after it, without looking ahead
and without counting data gaps as checked stops.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import src.live.runner as runner
from src.backtest.engine import MarketBar
from src.execution.plan import Order, OrderKind
from src.live.paper_venue import MarketPaperVenue
from src.live.runner import PAPER_DECISION_DELAY, advance, paper_as_of

D = Decimal
H = timedelta(hours=1)
MIDNIGHT = datetime(2026, 9, 27, tzinfo=UTC)


def test_on_time_run_executes_now():
    now = MIDNIGHT + timedelta(minutes=7)
    assert paper_as_of(now) == now


def test_seconds_after_the_nominal_time_are_still_on_time():
    """00:10:11 is on time: the night run of 2026-09-28 was marked late."""
    now = MIDNIGHT + timedelta(minutes=10, seconds=11)
    assert paper_as_of(now) == now
    assert paper_as_of(MIDNIGHT + timedelta(minutes=59)) == MIDNIGHT + timedelta(minutes=59)


def test_late_run_executes_at_the_decision_moment():
    assert paper_as_of(MIDNIGHT + timedelta(hours=5, minutes=27)) == MIDNIGHT + PAPER_DECISION_DELAY
    assert paper_as_of(MIDNIGHT + timedelta(hours=22)) == MIDNIGHT + PAPER_DECISION_DELAY


def bar(ts: datetime, low: str, close: str) -> MarketBar:
    return MarketBar(ts, D(close), D(close), D(low), D(close), D(1))


async def held_venue() -> MarketPaperVenue:
    v = MarketPaperVenue(mark={"AUSDT": D(100), "BUSDT": D(100)})
    for sym in ("AUSDT", "BUSDT"):
        await v.place(Order(OrderKind.ENTRY, sym, True, D(1), None, False, f"e-{sym}"))
        await v.place(Order(OrderKind.STOP_LOSS, sym, False, D(1), D(90), True, f"s-{sym}"))
    v.replayed_until = MIDNIGHT
    return v


async def test_stop_after_the_decision_is_caught(monkeypatch):
    series = {
        "AUSDT": [bar(MIDNIGHT + H * i, "95", "96") for i in range(5)]
        + [bar(MIDNIGHT + H * 5, "85", "88")],
        "BUSDT": [bar(MIDNIGHT + H * i, "99", "100") for i in range(6)],
    }

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in series[symbol] if start <= b.ts < end]

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    v = await held_venue()
    events = await advance(v, MIDNIGHT + H * 6 + timedelta(minutes=30))
    assert [p.symbol for p in await v.positions()] == ["BUSDT"]
    assert any("AUSDT" in e for e in events)
    assert v.replayed_until == MIDNIGHT + H * 6


async def test_hole_in_one_symbol_is_not_counted_as_checked(monkeypatch):
    series = {
        "AUSDT": [bar(MIDNIGHT + H * i, "95", "96") for i in range(6)],
        "BUSDT": [bar(MIDNIGHT + H * i, "99", "100") for i in range(2)],  # data ran out
    }

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in series[symbol] if start <= b.ts < end]

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    v = await held_venue()
    await advance(v, MIDNIGHT + H * 6)
    # The mark goes no further than the MOST lagging held symbol was replayed.
    assert v.replayed_until == MIDNIGHT + H * 2


# ── retry of the same day (third review round 2026-09-28) ───
from src.core.config import load_settings  # noqa: E402
from src.execution.plan import InstrumentSpec  # noqa: E402
from src.live.runner import paper_venue  # noqa: E402


def hourly(start_price: str, hours: int, low: dict[int, str] | None = None) -> list[MarketBar]:
    low = low or {}
    out = []
    for i in range(-3, hours):
        ts = MIDNIGHT + H * i
        out.append(MarketBar(ts, D(start_price), D(start_price), D(low.get(i, start_price)),
                             D(start_price), D(1)))
    return out


async def test_retry_after_the_venue_moved_past_the_decision_does_not_fail(monkeypatch, tmp_path):
    """The day's third attempt: the exchange is replayed to 01:00, the decision is at 00:10.
    Before: "no hourly bars" for the held position and a rejection."""
    series = {"AUSDT": hourly("100", 3), "BUSDT": hourly("50", 3)}

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in series[symbol] if start <= b.ts < end]

    async def fake_specs(names):
        return {
            n: InstrumentSpec(n, D("0.01"), D("0.001"), D("0.001"), D("1e6"), D(5)) for n in names
        }

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "instrument_specs", fake_specs)
    monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)
    held = await held_venue()
    held._positions.pop("BUSDT")
    held._resting = {k: o for k, o in held._resting.items() if o.symbol == "AUSDT"}
    held.replayed_until = MIDNIGHT + H  # the previous attempt reached 01:00
    held.save(tmp_path / "setup_e.json")

    venue, _, _ = await paper_venue(load_settings(), "setup_e", ["BUSDT"],
                                    MIDNIGHT + timedelta(minutes=10))
    assert venue.mark["BUSDT"] == D("50") and venue.mark["AUSDT"] == D("100")
    assert venue.replayed_until == MIDNIGHT + H, "what was replayed is not rolled back"
    assert venue.decision_passed, "the day's decision is already made: a retry does not enter"


async def test_on_time_first_run_is_not_marked_as_decided(monkeypatch, tmp_path):
    series = {"AUSDT": hourly("100", 1)}

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in series[symbol] if start <= b.ts < end]

    async def fake_specs(names):
        return {}

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "instrument_specs", fake_specs)
    monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)
    v = MarketPaperVenue()
    v.replayed_until = MIDNIGHT - H * 24  # yesterday's run
    v.save(tmp_path / "setup_e.json")
    venue, _, _ = await paper_venue(load_settings(), "setup_e", ["AUSDT"],
                                    MIDNIGHT + timedelta(minutes=10))
    assert not venue.decision_passed and venue.replayed_until == MIDNIGHT


async def test_held_position_needs_the_last_bar_before_the_decision(monkeypatch, tmp_path):
    """Sixth review round: the 23:00 bar lagged; before, a 2-hour tolerance let it
    through, the mark landed on 00:00, and the stop over it was never
    checked. Now it is a rejection: unknown whether the stop fired."""
    import pytest

    from src.live.runner import DataNotFresh

    series = {"AUSDT": [b for b in hourly("100", 1) if b.ts < MIDNIGHT - H]}  # until 22:00

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in series.get(symbol, []) if start <= b.ts < end]

    async def fake_specs(names):
        return {}

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "instrument_specs", fake_specs)
    monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)
    v = await held_venue()
    v._positions.pop("BUSDT")
    v.replayed_until = MIDNIGHT - H * 24
    v.save(tmp_path / "setup_e.json")
    with pytest.raises(DataNotFresh, match="AUSDT"):
        await paper_venue(load_settings(), "setup_e", [], MIDNIGHT + timedelta(minutes=10))


async def test_entry_price_must_be_at_the_decision_not_two_hours_old(monkeypatch, tmp_path):
    series = {"CUSDT": [b for b in hourly("10", 1) if b.ts < MIDNIGHT - H]}

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in series.get(symbol, []) if start <= b.ts < end]

    async def fake_specs(names):
        return {}

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    monkeypatch.setattr(runner, "instrument_specs", fake_specs)
    monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)
    venue, _, _ = await paper_venue(load_settings(), "setup_e", ["CUSDT"],
                                    MIDNIGHT + timedelta(minutes=10))
    assert "CUSDT" not in venue.mark, "the 22:00 price is not the 00:00 price (NO_PRICE)"
