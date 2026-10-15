"""Paper exchange on main-market prices. Phase 5, DEC-041.

Execution goes against us; otherwise the forward test would measure
the execution model, not the rule.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.backtest.engine import MarketBar
from src.execution.exchange import DuplicateOrder
from src.execution.plan import Order, OrderKind
from src.live.paper_venue import HOUR, MarketPaperVenue

D = Decimal
T0 = datetime(2026, 9, 21, tzinfo=UTC)


def bar(i: int, o: str, h: str, low: str, c: str) -> MarketBar:
    return MarketBar(T0 + HOUR * i, D(o), D(h), D(low), D(c), D("1"))


async def opened(long: bool = True) -> MarketPaperVenue:
    v = MarketPaperVenue(mark={"AUSDT": D("100")}, slippage_pct=D("0.1"))
    await v.place(Order(OrderKind.ENTRY, "AUSDT", long, D("1"), None, False, "e"))
    stop = D("90") if long else D("110")
    await v.place(Order(OrderKind.STOP_LOSS, "AUSDT", not long, D("1"), stop, True, "s"))
    return v


async def test_entry_pays_slippage_and_fee():
    v = await opened()
    fill = await v.fill("e", "AUSDT")
    assert fill.avg_price == D("100.1")
    assert fill.fee == D("100.1") * D("0.055") / 100


async def test_stop_is_checked_on_every_bar_not_only_at_the_run():
    """Price went below the stop and came back: by launch time the position is gone."""
    v = await opened()
    v.replay("AUSDT", [bar(0, "100", "101", "95", "96"), bar(1, "96", "97", "89", "93"),
                       bar(2, "93", "105", "92", "104")])
    assert await v.positions() == []
    assert v.mark["AUSDT"] == D("104")
    assert (await v.fill("s", "AUSDT")).avg_price == D("90") * D("0.999")


async def test_gap_through_the_stop_fills_at_the_open_not_the_level():
    v = await opened()
    v.replay("AUSDT", [bar(0, "80", "82", "78", "81")])
    assert (await v.fill("s", "AUSDT")).avg_price == D("80") * D("0.999")


async def test_short_stop_triggers_on_the_high():
    v = await opened(long=False)
    v.replay("AUSDT", [bar(0, "100", "108", "99", "104")])
    assert len(await v.positions()) == 1
    v.replay("AUSDT", [bar(1, "104", "111", "103", "109")])
    assert await v.positions() == []
    assert (await v.fill("s", "AUSDT")).avg_price == D("110") * D("1.001")


async def test_state_survives_a_restart_including_seen_keys(tmp_path):
    path = tmp_path / "paper.json"
    v = await opened()
    v.replayed_until = T0
    v.save(path)

    back = MarketPaperVenue.load(path, mark={"AUSDT": D("100")})
    assert await back.positions() == await v.positions()
    assert await back.open_orders() == await v.open_orders()
    assert await back.fill("e", "AUSDT") == await v.fill("e", "AUSDT")
    assert back.replayed_until == T0
    # The exchange remembers the entry key even after a restart; otherwise a retry
    # would open a second position.
    with pytest.raises(DuplicateOrder):
        await back.place(Order(OrderKind.ENTRY, "AUSDT", True, D("1"), None, False, "e"))


def test_missing_file_is_an_empty_venue(tmp_path):
    v = MarketPaperVenue.load(tmp_path / "none.json")
    assert v.held_symbols == [] and v.replayed_until is None

