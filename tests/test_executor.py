"""The Executor on a paper exchange that breaks like a real one.

Two guarantees, each checked on its own failure:

* one decision — at most one position, even when the exchange response
  is lost AFTER execution;
* a position without a stop does not live longer than one step.

An Executor tested only on an exchange that always responds
breaks exactly at the moment a response does not arrive.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.backtest.engine import Signal
from src.execution.exchange import PaperExchange
from src.execution.executor import open_position
from src.execution.plan import InstrumentSpec, OrderKind, OrderPlan, build
from src.execution.reconcile import Discrepancy

D = Decimal
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
SPEC = InstrumentSpec(
    symbol="SOLUSDT",
    tick_size=D("0.01"),
    qty_step=D("0.1"),
    min_order_qty=D("0.1"),
    max_order_qty=D("10000"),
    min_notional=D("5"),
)


def make_plan(is_long: bool = True) -> OrderPlan:
    signal = (
        Signal(is_long=True, entry=D("100"), stop=D("97"), target=D("106"))
        if is_long
        else Signal(is_long=False, entry=D("100"), stop=D("103"), target=D("94"))
    )
    plan = build(signal, spec=SPEC, qty=D("1"), moment=T0)
    assert isinstance(plan, OrderPlan)
    return plan


def exchange() -> PaperExchange:
    return PaperExchange(mark={"SOLUSDT": D("100")})


# ---------------------------------------------------------------------------
# Normal path
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_happy_path_ends_with_a_protected_position_and_clean_state():
    ex = exchange()
    plan = make_plan()
    report = await open_position(ex, plan, moment=T0)

    assert report.entered and report.protected
    assert not report.halt
    assert report.reconciliation.describe() == "state matches"
    assert {o.link_id for o in await ex.open_orders()} == {
        plan.stop_loss.link_id,
        plan.take_profit.link_id,
    }


@pytest.mark.asyncio
async def test_stop_is_placed_before_the_target():
    """The target is money we might fail to earn; the stop is money
    we might lose."""
    ex = exchange()
    await open_position(ex, make_plan(), moment=T0)
    placed = [line for line in ex.log if line.startswith("placed")]
    assert placed[0].startswith(f"placed {OrderKind.STOP_LOSS.value}")


# ---------------------------------------------------------------------------
# Lost response — the main failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lost_entry_response_does_not_open_a_second_position():
    """The response is lost AFTER execution. The exchange rejects a retry with the same key
    as a duplicate — and that is a confirmation, not an error."""
    ex = exchange()
    plan = make_plan()
    ex.lose_response_for.add(plan.entry.link_id)

    report = await open_position(ex, plan, moment=T0)

    positions = await ex.positions()
    assert len(positions) == 1
    assert positions[0].qty == plan.qty
    assert report.entered and report.protected
    assert not report.halt
    assert sum(1 for line in ex.log if line.startswith("entry filled")) == 1


@pytest.mark.asyncio
async def test_lost_stop_response_still_counts_as_protected():
    ex = exchange()
    plan = make_plan()
    ex.lose_response_for.add(plan.stop_loss.link_id)

    report = await open_position(ex, plan, moment=T0)
    assert report.protected
    assert not report.halt


@pytest.mark.asyncio
async def test_rerunning_the_whole_open_is_idempotent():
    """The process crashed after opening and restarted with the same decision.
    No second position appears."""
    ex = exchange()
    plan = make_plan()
    await open_position(ex, plan, moment=T0)
    second = await open_position(ex, plan, moment=T0)

    assert len(await ex.positions()) == 1
    assert second.entered and second.protected
    assert not second.halt


# ---------------------------------------------------------------------------
# Position without a stop
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unplaceable_stop_closes_the_position_and_halts():
    """Waiting for a human is more dangerous than acting: an unprotected leveraged
    position loses money every second."""
    ex = exchange()
    plan = make_plan()
    ex.reject.add(plan.stop_loss.link_id)

    report = await open_position(ex, plan, moment=T0)

    assert report.entered
    assert not report.protected
    assert report.closed_by_emergency
    assert report.halt
    assert await ex.positions() == []
    assert await ex.open_orders() == []


@pytest.mark.asyncio
async def test_failed_emergency_close_is_reported_loudly():
    ex = exchange()
    plan = make_plan()
    ex.reject.add(plan.stop_loss.link_id)
    from src.execution.plan import emergency_close

    ex.reject.add(emergency_close(plan, T0).link_id)

    report = await open_position(ex, plan, moment=T0)

    assert not report.closed_by_emergency
    assert report.halt
    assert any("POSITION WITHOUT A STOP" in note for note in report.notes)
    kinds = {f.kind for f in report.reconciliation.findings}
    assert Discrepancy.ORPHAN_POSITION in kinds


# ---------------------------------------------------------------------------
# Other rejections
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rejected_entry_leaves_nothing_behind():
    ex = exchange()
    plan = make_plan()
    ex.reject.add(plan.entry.link_id)

    report = await open_position(ex, plan, moment=T0)
    assert not report.entered
    assert await ex.positions() == []
    assert await ex.open_orders() == []
    assert not report.halt


@pytest.mark.asyncio
async def test_missing_target_halts_but_keeps_the_stop():
    """Without a target the position is protected, but the state diverges from the plan."""
    ex = exchange()
    plan = make_plan()
    ex.reject.add(plan.take_profit.link_id)

    report = await open_position(ex, plan, moment=T0)
    assert report.protected
    assert report.halt
    assert plan.stop_loss.link_id in {o.link_id for o in await ex.open_orders()}


@pytest.mark.asyncio
async def test_entry_that_never_confirms_halts():
    """Responses were lost until the attempts ran out, and there is no position on the exchange:
    we do not know what is happening and do not go further."""

    class Silent(PaperExchange):
        async def place(self, order):  # noqa: ANN001
            from src.execution.exchange import LostResponse

            raise LostResponse(order.link_id)

    ex = Silent(mark={"SOLUSDT": D("100")})
    report = await open_position(ex, make_plan(), moment=T0)
    assert not report.entered
    assert any("no position on the expected side" in n for n in report.notes)


# ---------------------------------------------------------------------------
# The paper exchange behaves like a real one
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stop_hit_closes_position_and_cancels_the_target():
    ex = exchange()
    plan = make_plan()
    await open_position(ex, plan, moment=T0)
    ex.move_price("SOLUSDT", D("96"))
    assert await ex.positions() == []
    assert await ex.open_orders() == []


@pytest.mark.asyncio
async def test_short_side_works_end_to_end():
    ex = exchange()
    report = await open_position(ex, make_plan(is_long=False), moment=T0)
    assert report.entered and report.protected and not report.halt
    ex.move_price("SOLUSDT", D("93"))
    assert await ex.positions() == []


# ---------------------------------------------------------------------------
# Time exit (DEC-041)
# ---------------------------------------------------------------------------
def time_exit_plan() -> OrderPlan:
    plan = build(
        Signal(is_long=True, entry=D("100"), stop=D("80"), target=None),
        spec=SPEC, qty=D("1"), moment=T0,
    )
    assert isinstance(plan, OrderPlan)
    return plan


@pytest.mark.asyncio
async def test_time_exit_position_opens_with_only_a_stop():
    ex = exchange()
    plan = time_exit_plan()
    report = await open_position(ex, plan, moment=T0)
    assert report.entered and report.protected and not report.halt
    assert [o.link_id for o in await ex.open_orders()] == [plan.stop_loss.link_id]


@pytest.mark.asyncio
async def test_time_close_closes_first_then_removes_the_stop():
    """The other way round — and if closing failed, the position would be left unprotected.

    What is checked is the order of the executor's CALLS, not the exchange log: the paper
    exchange cancels the position's orders together with it, and a "cancelled" line in its
    log may not exist at all.
    """
    from datetime import timedelta

    from src.execution.executor import close_by_time

    class Recording(PaperExchange):
        calls: list[str] = []

        async def place(self, order):  # noqa: ANN001
            self.calls.append(f"place {order.kind.value}")
            return await super().place(order)

        async def cancel(self, link_id):  # noqa: ANN001
            self.calls.append(f"cancel {link_id}")
            return await super().cancel(link_id)

    ex = Recording(mark={"SOLUSDT": D("100")})
    ex.calls = []
    plan = time_exit_plan()
    await open_position(ex, plan, moment=T0)
    result = await close_by_time(ex, plan, moment=T0 + timedelta(days=7))

    assert result.closed and result.stop_cancelled
    assert await ex.positions() == []
    assert await ex.open_orders() == []
    close_at = ex.calls.index(f"place {OrderKind.TIME_CLOSE.value}")
    cancel_at = ex.calls.index(f"cancel {plan.stop_loss.link_id}")
    assert close_at < cancel_at


@pytest.mark.asyncio
async def test_failed_time_close_keeps_the_stop():
    from datetime import timedelta

    from src.execution.executor import close_by_time
    from src.execution.plan import time_close

    ex = exchange()
    plan = time_exit_plan()
    await open_position(ex, plan, moment=T0)
    moment = T0 + timedelta(days=7)
    ex.reject.add(time_close(plan, moment).link_id)

    result = await close_by_time(ex, plan, moment=moment)
    assert not result.closed
    assert plan.stop_loss.link_id in {o.link_id for o in await ex.open_orders()}
    assert len(await ex.positions()) == 1


@pytest.mark.asyncio
async def test_time_close_after_the_stop_already_fired():
    """A stop closed the position before the deadline: nothing to close, orders cancelled."""
    from datetime import timedelta

    from src.execution.executor import close_by_time

    ex = exchange()
    plan = time_exit_plan()
    await open_position(ex, plan, moment=T0)
    ex.move_price("SOLUSDT", D("75"))
    result = await close_by_time(ex, plan, moment=T0 + timedelta(days=7))
    assert result.closed
    assert await ex.open_orders() == []


async def test_second_entry_does_not_see_the_first_as_foreign():
    """Reconciliation after the second entry knows about the first position."""
    from src.execution.reconcile import LocalPosition

    ex = PaperExchange(mark={"SOLUSDT": D("100"), "ETHUSDT": D("2000")})
    first = make_plan()
    report = await open_position(ex, first, moment=T0)
    assert not report.halt
    held = [
        LocalPosition(
            "SOLUSDT", True, first.qty, first.stop_loss.link_id,
            target_link_id=first.take_profit.link_id,
        )
    ]

    eth_spec = InstrumentSpec("ETHUSDT", D("0.01"), D("0.01"), D("0.01"), D("1000"), D("5"))
    second = build(
        Signal(is_long=False, entry=D("2000"), stop=D("2100"), target=None),
        spec=eth_spec, qty=D("0.01"), moment=T0,
    )
    assert isinstance(second, OrderPlan)
    report = await open_position(ex, second, moment=T0, held=held)
    assert not report.halt, report.reconciliation.describe()
    assert not report.reconciliation.findings, report.reconciliation.describe()

    # Without knowing about the first — a discrepancy, as it was before the fix.
    third_ex = PaperExchange(mark={"SOLUSDT": D("100"), "ETHUSDT": D("2000")})
    await open_position(third_ex, first, moment=T0)
    report = await open_position(third_ex, second, moment=T0)
    assert report.halt
