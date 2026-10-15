"""Order plan. Spec §4, §8.

This is the code that decides exactly what goes to the exchange. What is checked
is not "whether a plan is built" but two properties where an error costs money
while looking harmless: the rounding direction and idempotency.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import Signal
from src.core.models import RejectionCode
from src.execution.plan import (
    InstrumentSpec,
    OrderKind,
    OrderPlan,
    PlanRejected,
    build,
    link_id,
    round_qty_down,
    round_to_tick,
)

D = Decimal
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def spec(**overrides) -> InstrumentSpec:
    base = {
        "symbol": "SOLUSDT",
        "tick_size": D("0.01"),
        "qty_step": D("0.1"),
        "min_order_qty": D("0.1"),
        "max_order_qty": D("10000"),
        "min_notional": D("5"),
    }
    base.update(overrides)
    return InstrumentSpec(**base)


def long_signal(entry="100.004", stop="97.004", target="106.004") -> Signal:
    return Signal(is_long=True, entry=D(entry), stop=D(stop), target=D(target))


def short_signal(entry="100.004", stop="103.004", target="94.004") -> Signal:
    return Signal(is_long=False, entry=D(entry), stop=D(stop), target=D(target))


def plan(signal=None, *, qty="1", s=None, moment=T0):
    return build(
        signal or long_signal(), spec=s or spec(), qty=D(qty), moment=moment
    )


# ---------------------------------------------------------------------------
# Price rounding
# ---------------------------------------------------------------------------
def test_round_to_tick_goes_where_asked():
    assert round_to_tick(D("100.004"), D("0.01"), up=True) == D("100.01")
    assert round_to_tick(D("100.004"), D("0.01"), up=False) == D("100.00")
    assert round_to_tick(D("100.00"), D("0.01"), up=True) == D("100.00")


def test_round_to_tick_refuses_a_non_positive_step():
    with pytest.raises(ValueError):
        round_to_tick(D("100"), D("0"), up=True)


def test_long_stop_moves_TOWARD_the_entry():
    """Further would mean a loss larger than planned, i.e. a quiet breach
    of the limit the Risk Manager has just computed."""
    result = plan(long_signal(entry="100.004", stop="97.004"))
    assert isinstance(result, OrderPlan)
    assert result.entry_price == D("100.01")      # entry against us
    assert result.risk_per_unit <= D("3")         # the given distance


def test_long_target_moves_AWAY_from_the_entry():
    """Closer would mean the target is more attainable than in the backtest."""
    result = plan(long_signal(entry="100.004", target="106.004"))
    assert result.target_price - result.entry_price >= D("6")


def test_short_mirrors_both_directions():
    result = plan(short_signal())
    assert isinstance(result, OrderPlan)
    assert result.stop_price > result.entry_price > result.target_price
    assert result.risk_per_unit <= D("3")
    assert result.entry_price - result.target_price >= D("6")


@pytest.mark.parametrize("tick", ["0.0001", "0.01", "0.1", "0.5", "1", "2.5"])
@pytest.mark.parametrize("offset", ["0", "0.003", "0.337", "0.999"])
def test_rounding_never_widens_the_planned_risk(tick, offset):
    """The main property, and it is proven rather than hoped for.

    The actual risk is no larger than the one set by the signal, and the actual target
    is no closer than the given one — for any price step and any offset of the levels
    relative to the grid.
    """
    shift = D(offset)
    for signal in (
        Signal(
            is_long=True,
            entry=D("100") + shift,
            stop=D("97") + shift,
            target=D("106") + shift,
        ),
        Signal(
            is_long=False,
            entry=D("100") + shift,
            stop=D("103") + shift,
            target=D("94") + shift,
        ),
    ):
        result = build(signal, spec=spec(tick_size=D(tick)), qty=D("50"), moment=T0)
        if isinstance(result, PlanRejected):
            continue
        assert result.risk_per_unit <= abs(signal.entry - signal.stop) + D("1e-27")
        reward = abs(result.target_price - result.entry_price)
        assert reward >= abs(signal.target - signal.entry) - D("1e-27")


# ---------------------------------------------------------------------------
# Degenerate cases after rounding
# ---------------------------------------------------------------------------
def test_stop_collapsing_onto_the_entry_is_refused():
    """On an instrument with a coarse step this is a real case,
    not paranoia."""
    signal = Signal(is_long=True, entry=D("100"), stop=D("99.6"), target=D("110"))
    result = build(signal, spec=spec(tick_size=D("1")), qty=D("1"), moment=T0)
    assert isinstance(result, PlanRejected)
    assert result.rejection is RejectionCode.LIQ_BUFFER


def test_degenerate_signal_is_refused():
    """A zero distance to the stop or to the target is not a plan but an error."""
    for signal in (
        Signal(is_long=True, entry=D("100"), stop=D("100"), target=D("110")),
        Signal(is_long=True, entry=D("100"), stop=D("90"), target=D("100")),
    ):
        result = build(signal, spec=spec(), qty=D("1"), moment=T0)
        assert isinstance(result, PlanRejected)
        assert result.rejection is RejectionCode.LIQ_BUFFER


# ---------------------------------------------------------------------------
# Quantity and instrument limits
# ---------------------------------------------------------------------------
def test_qty_rounds_down_only():
    """Rounding up would increase the position beyond the computed one."""
    assert round_qty_down(D("1.99"), D("0.1")) == D("1.9")
    assert round_qty_down(D("2.0"), D("0.1")) == D("2.0")
    result = plan(qty="1.99")
    assert result.qty == D("1.9")


def test_qty_below_the_minimum_lot_is_refused():
    result = plan(qty="0.05")
    assert isinstance(result, PlanRejected)
    assert result.rejection is RejectionCode.MIN_NOTIONAL


def test_qty_above_the_maximum_lot_is_refused():
    result = plan(qty="20000")
    assert isinstance(result, PlanRejected)


def test_notional_below_the_exchange_minimum_is_refused():
    result = build(
        long_signal(), spec=spec(min_notional=D("500")), qty=D("1"), moment=T0
    )
    assert isinstance(result, PlanRejected)
    assert result.rejection is RejectionCode.MIN_NOTIONAL


def test_arithmetic_blowup_is_a_rejection_not_a_crash():
    signal = Signal(is_long=True, entry=D("NaN"), stop=D("90"), target=D("110"))
    result = build(signal, spec=spec(), qty=D("1"), moment=T0)
    assert isinstance(result, PlanRejected)
    assert result.rejection is RejectionCode.EXCHANGE_ERROR


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------
def test_same_decision_gives_the_same_keys():
    """A rerun after a dropped connection must produce THE SAME keys,
    otherwise the exchange accepts the second order as new and opens a second position
    on top of the first."""
    first = plan()
    second = plan()
    assert [o.link_id for o in first.orders] == [o.link_id for o in second.orders]


def test_keys_differ_by_role_symbol_and_moment():
    base = link_id("SOLUSDT", T0, OrderKind.ENTRY)
    assert base != link_id("SOLUSDT", T0, OrderKind.STOP_LOSS)
    assert base != link_id("BTCUSDT", T0, OrderKind.ENTRY)
    assert base != link_id("SOLUSDT", T0 + timedelta(minutes=1), OrderKind.ENTRY)


def test_keys_are_short_enough_for_the_exchange():
    """Bybit limits orderLinkId; a long key is rejected
    on submission, i.e. at the worst possible moment."""
    key = link_id("1000000BABYDOGEUSDT", T0, OrderKind.TAKE_PROFIT)
    assert len(key) <= 36
    assert key.replace("-", "").isalnum()


# ---------------------------------------------------------------------------
# Order shape
# ---------------------------------------------------------------------------
def test_exits_are_reduce_only_and_face_the_other_way():
    """Without reduce_only a partial stop fill on an already closed
    position would open an opposite one."""
    result = plan()
    assert result.entry.buy is True
    assert not result.entry.reduce_only
    assert result.entry.price is None

    for exit_order in (result.stop_loss, result.take_profit):
        assert exit_order.buy is False
        assert exit_order.reduce_only is True
        assert exit_order.price is not None


def test_short_entry_sells():
    result = plan(short_signal())
    assert result.entry.buy is False
    assert result.stop_loss.buy is True
    assert result.take_profit.buy is True


def test_all_three_orders_share_the_quantity():
    result = plan(qty="3.37")
    assert {o.qty for o in result.orders} == {result.qty}
    assert result.notional == result.qty * result.entry_price
