"""Opening a position: from the plan to a protected position. Spec §8.

Two guarantees, both tested on a paper exchange that can break
the same way a real one does.

**One decision — at most one position.** The exchange response can get
lost AFTER the fill. The exchange rejects a retry with the same `link_id`
as a duplicate — and that is not an error but a confirmation that
the first attempt went through. An Executor that answers a duplicate with
a new key opens a second position on top of the first.

**A position without a stop does not live longer than one step.** The stop is placed
right after the entry is confirmed and before the target: the target is money
we might not earn, the stop is money we might lose.
If the stop cannot be placed, the position is closed at market, and only
then is trading disabled (P-001, DEC-027).

After all that — a mandatory reconciliation with the exchange. The Executor trusts
not its own responses but what the exchange shows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from src.execution.exchange import (
    DuplicateOrder,
    Exchange,
    LostResponse,
    RejectedOrder,
)
from src.execution.plan import Order, OrderPlan, emergency_close
from src.execution.reconcile import LocalPosition, Reconciliation, reconcile


@dataclass
class ExecutionReport:
    plan: OrderPlan
    entered: bool = False
    protected: bool = False
    closed_by_emergency: bool = False
    reconciliation: Reconciliation | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def halt(self) -> bool:
        """Whether trading must be disabled after this opening."""
        if self.closed_by_emergency:
            return True
        return bool(self.reconciliation and self.reconciliation.halt_required)


async def _send(exchange: Exchange, order: Order, *, attempts: int) -> str:
    """Sending with retries. Returns an outcome instead of raising.

    'placed'   — the order is definitely on the exchange;
    'unknown'  — responses kept getting lost until attempts ran out; what is on
                 the exchange only reconciliation knows;
    'rejected' — the exchange rejected on the merits.
    """
    for _ in range(max(1, attempts)):
        try:
            await exchange.place(order)
            return "placed"
        except DuplicateOrder:
            # The previous attempt went through, the response got lost. This is success.
            return "placed"
        except LostResponse:
            # Retry with THE SAME key: if the order went through, the exchange
            # answers with a duplicate; if not, it fills it.
            continue
        except RejectedOrder:
            return "rejected"
    return "unknown"


async def _snapshot(
    exchange: Exchange, local: list[LocalPosition]
) -> Reconciliation:
    """Reconciliation against what the exchange shows NOW, not against our responses."""
    return reconcile(local, await exchange.positions(), await exchange.open_orders())


async def open_position(
    exchange: Exchange,
    plan: OrderPlan,
    *,
    moment: datetime,
    attempts: int = 3,
    held: Sequence[LocalPosition] = (),
) -> ExecutionReport:
    """Entry, then stop, then target — and reconciliation.

    `held` — positions that are already open and known to us. Without them
    reconciliation after the second entry would see the first position as foreign
    and disable trading out of nowhere.
    """
    report = ExecutionReport(plan=plan)
    known = list(held)

    entry = await _send(exchange, plan.entry, attempts=attempts)
    positions = {p.symbol: p for p in await exchange.positions()}
    held = positions.get(plan.symbol)
    report.entered = held is not None and held.is_long == plan.is_long

    if entry == "rejected" and held is None:
        report.notes.append("entry rejected by the exchange, no position")
        report.reconciliation = await _snapshot(exchange, known)
        return report
    if not report.entered:
        # We sent the entry, but there is no position (or it is on the other side).
        # Our model of the world is wrong — only reconciliation and shutdown from here.
        report.notes.append(f"entry: outcome {entry}, but no position on the expected side")
        report.reconciliation = await _snapshot(exchange, known)
        return report

    stop = await _send(exchange, plan.stop_loss, attempts=attempts)
    live = {o.link_id for o in await exchange.open_orders()}
    report.protected = plan.stop_loss.link_id in live

    if not report.protected:
        # The position is open and unprotected. Waiting for a human is more dangerous
        # than acting: close at market, then shut down.
        report.notes.append(f"stop not placed (outcome {stop}) — closing at market")
        closing = await _send(exchange, emergency_close(plan, moment), attempts=attempts)
        still = {p.symbol for p in await exchange.positions()}
        report.closed_by_emergency = plan.symbol not in still
        if not report.closed_by_emergency:
            report.notes.append(
                f"emergency close not confirmed (outcome {closing}) — "
                "POSITION WITHOUT A STOP, a human is needed"
            )
        report.reconciliation = await _snapshot(exchange, known)
        return report

    if plan.take_profit is not None:
        target = await _send(exchange, plan.take_profit, attempts=attempts)
        if target != "placed":
            report.notes.append(f"target not placed (outcome {target})")

    report.reconciliation = await _snapshot(
        exchange,
        [
            *known,
            LocalPosition(
                symbol=plan.symbol,
                is_long=plan.is_long,
                qty=plan.qty,
                stop_link_id=plan.stop_loss.link_id,
                target_link_id=plan.take_profit.link_id if plan.take_profit else None,
            )
        ],
    )
    return report


@dataclass
class CloseReport:
    closed: bool = False
    stop_cancelled: bool = False
    notes: list[str] = field(default_factory=list)


async def close_by_time(
    exchange: Exchange,
    plan: OrderPlan,
    *,
    moment: datetime,
    attempts: int = 3,
) -> CloseReport:
    """Closing when the holding window expires.

    The order is part of the contract: FIRST close the position and confirm on the
    exchange that it is gone, and only THEN cancel the protective stop. The other way
    round, a failed close would leave the position unprotected.
    """
    from src.execution.plan import time_close

    report = CloseReport()
    held = {p.symbol for p in await exchange.positions()}
    if plan.symbol in held:
        outcome = await _send(exchange, time_close(plan, moment), attempts=attempts)
        still = {p.symbol for p in await exchange.positions()}
        report.closed = plan.symbol not in still
        if not report.closed:
            report.notes.append(
                f"time close not confirmed (outcome {outcome}) — stop left in place"
            )
            return report
    else:
        # The position is already gone — the stop closed it. Only orders remain to cancel.
        report.closed = True
        report.notes.append("no position on the exchange anymore — closed earlier")

    await exchange.cancel(plan.stop_loss.link_id)
    if plan.take_profit is not None:
        await exchange.cancel(plan.take_profit.link_id)
    live = {o.link_id for o in await exchange.open_orders()}
    report.stop_cancelled = plan.stop_loss.link_id not in live
    return report


__all__ = ["CloseReport", "ExecutionReport", "close_by_time", "open_position"]
