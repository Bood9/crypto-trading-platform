"""Reconciliation of our view of positions with the exchange. Spec §8, P-001.

**The exchange holds the truth, always.** Our state is the memory of a process
that could have crashed between sending an order and recording the result,
got a timeout on a successfully filled order, or slept through
a partial fill. The exchange, on the other hand, knows what is really there.

Hence the rule: on any discrepancy we cannot explain, trading
is disabled and waits for a human. Not "adapt to the exchange
and carry on" — a discrepancy means our model of the world is wrong,
and the next decision would be made on a wrong model.

There is exactly one exception, and it goes the other way: **a position without a stop**.
Here waiting for a human is more dangerous than acting — an unprotected leveraged
position loses money every second. The stop is placed immediately,
and only then is trading disabled.

The module is pure: no network, no DB. Input — two snapshots, output —
a list of discrepancies with prescribed actions. Code that decides
whether something does not match with real money must be tested
in full.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from src.execution.plan import OrderKind


class Discrepancy(StrEnum):
    """What exactly does not match. A closed enumeration (P-001)."""

    #: The position exists on the exchange, we have no record of it.
    ORPHAN_POSITION = "ORPHAN_POSITION"
    #: We have the position, the exchange does not.
    PHANTOM_POSITION = "PHANTOM_POSITION"
    #: Both have the position, but with different sizes.
    SIZE_MISMATCH = "SIZE_MISMATCH"
    #: Both have the position, but on different sides.
    SIDE_MISMATCH = "SIDE_MISMATCH"
    #: An open position without a protective stop.
    UNPROTECTED = "UNPROTECTED"
    #: An order is on the exchange that we did not place.
    ORPHAN_ORDER = "ORPHAN_ORDER"
    #: An order we consider placed is missing on the exchange.
    MISSING_ORDER = "MISSING_ORDER"


class Action(StrEnum):
    """What to do about it."""

    #: Disable trading and wait for a human.
    HALT = "HALT"
    #: Immediately place a protective stop, then disable trading.
    PLACE_STOP_THEN_HALT = "PLACE_STOP_THEN_HALT"
    #: Cancel the foreign order. By itself it does not stop trading.
    CANCEL = "CANCEL"


@dataclass(frozen=True)
class ExchangePosition:
    symbol: str
    is_long: bool
    qty: Decimal
    entry_price: Decimal


@dataclass(frozen=True)
class ExchangeOrder:
    link_id: str
    symbol: str
    qty: Decimal
    reduce_only: bool


@dataclass(frozen=True)
class LocalPosition:
    """A position as our memory sees it, together with its order keys."""

    symbol: str
    is_long: bool
    qty: Decimal
    stop_link_id: str
    #: None — time exit only, there is no target order and we do not expect one.
    target_link_id: str | None = None

    @property
    def expected_orders(self) -> frozenset[str]:
        return frozenset(x for x in (self.stop_link_id, self.target_link_id) if x)


@dataclass(frozen=True)
class Finding:
    kind: Discrepancy
    symbol: str
    action: Action
    note: str


@dataclass
class Reconciliation:
    findings: list[Finding] = field(default_factory=list)

    @property
    def halt_required(self) -> bool:
        return any(
            f.action in (Action.HALT, Action.PLACE_STOP_THEN_HALT)
            for f in self.findings
        )

    @property
    def unprotected(self) -> list[Finding]:
        """Positions without a stop. Returned separately because
        the action for them is urgent, not just "shut down"."""
        return [f for f in self.findings if f.kind is Discrepancy.UNPROTECTED]

    @property
    def to_cancel(self) -> list[Finding]:
        return [f for f in self.findings if f.action is Action.CANCEL]

    def describe(self) -> str:
        if not self.findings:
            return "state matches"
        return "; ".join(f"{f.kind.value} {f.symbol}: {f.note}" for f in self.findings)


#: Tolerance for comparing quantities. The exchange rounds to the lot step,
#: and exact Decimal equality here would catch a difference in the last
#: digit as a state discrepancy.
QTY_TOLERANCE = Decimal("1e-12")


def reconcile(
    local: list[LocalPosition],
    positions: list[ExchangePosition],
    orders: list[ExchangeOrder],
    *,
    qty_tolerance: Decimal = QTY_TOLERANCE,
) -> Reconciliation:
    """Reconciliation of two snapshots.

    The order of the checks does not matter — findings do not exclude one
    another, and the report must contain all of them. Hiding the second finding
    behind the first would mean solving problems one at a time, while they
    usually share a cause.
    """
    result = Reconciliation()
    ours = {p.symbol: p for p in local}
    theirs = {p.symbol: p for p in positions}
    live_orders = {o.link_id: o for o in orders}

    for symbol, position in sorted(theirs.items()):
        mine = ours.get(symbol)
        if mine is None:
            result.findings.append(
                Finding(
                    Discrepancy.ORPHAN_POSITION,
                    symbol,
                    Action.HALT,
                    f"{position.qty} open on the exchange "
                    f"{'long' if position.is_long else 'short'}, we have no record",
                )
            )
            continue

        if mine.is_long != position.is_long:
            result.findings.append(
                Finding(
                    Discrepancy.SIDE_MISMATCH,
                    symbol,
                    Action.HALT,
                    f"we have {'long' if mine.is_long else 'short'}, "
                    f"exchange has {'long' if position.is_long else 'short'}",
                )
            )
        elif abs(mine.qty - position.qty) > qty_tolerance:
            result.findings.append(
                Finding(
                    Discrepancy.SIZE_MISMATCH,
                    symbol,
                    Action.HALT,
                    f"we have {mine.qty}, exchange has {position.qty} — "
                    "possibly a partial fill",
                )
            )

        # Position protection is checked SEPARATELY from size matching:
        # a position can match to the last digit and still hang without a stop.
        if mine.stop_link_id not in live_orders:
            result.findings.append(
                Finding(
                    Discrepancy.UNPROTECTED,
                    symbol,
                    Action.PLACE_STOP_THEN_HALT,
                    f"position {position.qty} open, stop "
                    f"{mine.stop_link_id} missing on the exchange",
                )
            )
        if mine.target_link_id is not None and mine.target_link_id not in live_orders:
            result.findings.append(
                Finding(
                    Discrepancy.MISSING_ORDER,
                    symbol,
                    Action.HALT,
                    f"target order {mine.target_link_id} missing",
                )
            )

    for symbol, mine in sorted(ours.items()):
        if symbol in theirs:
            continue
        result.findings.append(
            Finding(
                Discrepancy.PHANTOM_POSITION,
                symbol,
                Action.HALT,
                f"we consider {mine.qty} open, no position on the exchange — "
                "possibly closed by the stop without our knowledge",
            )
        )

    expected = {link for p in local for link in p.expected_orders}
    for link, order in sorted(live_orders.items()):
        if link in expected:
            continue
        # An order with no position and no record of ours — foreign or left over
        # from a previous life of the process. It is cancelled: a hanging reduce-only
        # order would open an opposite position if the position is already closed.
        result.findings.append(
            Finding(
                Discrepancy.ORPHAN_ORDER,
                order.symbol,
                Action.CANCEL,
                f"order {link} for {order.qty} does not belong to us",
            )
        )

    return result


def protective_stop_of(plan_orders: dict[OrderKind, str]) -> str | None:
    """The protective stop key from the plan's key set."""
    return plan_orders.get(OrderKind.STOP_LOSS)


__all__ = [
    "QTY_TOLERANCE",
    "Action",
    "Discrepancy",
    "ExchangeOrder",
    "ExchangePosition",
    "Finding",
    "LocalPosition",
    "Reconciliation",
    "protective_stop_of",
    "reconcile",
]
