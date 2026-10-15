"""Order plan: from intent to what goes to the exchange. Spec §4, §8.

The signal sets the levels, the Risk Manager the size. Between them and the exchange
remains a layer that did not exist until now: prices must be put on the instrument
tick, the quantity on the lot step, and every order needs a key
so that a retry does not create a second position.

The module is pure: no network, no DB, no clock, no randomness. This is the code
that decides WHAT exactly goes to the exchange, and it must be
fully testable and reproducible down to the last digit.

**Rounding is always conservative, and the directions differ.**

* The entry goes AGAINST us: a long buys higher, a short sells lower.
* The stop goes CLOSER to the entry than specified. Farther would mean a loss larger
  than planned, i.e. a silent breach of the limit the
  Risk Manager has just computed.
* The target goes FARTHER from the entry. Closer would make it more reachable
  than in the backtest, and the computation would stop matching what is traded.

Both exit prices are built from the ALREADY ROUNDED entry rather than rounded
separately. Two independently rounded levels drift apart by a tick,
and the actual risk comes out wider than specified — a property one wants
to prove rather than hope for.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from src.backtest.engine import Signal
from src.core.models import RejectionCode


class OrderKind(StrEnum):
    """The order's role in the position. A closed enumeration (P-001)."""

    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    #: Close when the holding window expires (DEC-033, DEC-041).
    TIME_CLOSE = "TIME_CLOSE"
    #: Market close when the protective stop could not be placed.
    #: The only order the Executor sends without a plan:
    #: a position without a stop is worse than any closing price.
    EMERGENCY_CLOSE = "EMERGENCY_CLOSE"


@dataclass(frozen=True)
class InstrumentSpec:
    """Instrument constraints. Only from the API cache — hardcoding is forbidden
    (spec §16)."""

    symbol: str
    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class Order:
    kind: OrderKind
    symbol: str
    #: Side on the exchange: True — buy.
    buy: bool
    qty: Decimal
    #: None for a market entry: the market sets the price.
    price: Decimal | None
    #: Closes the position and cannot open or increase it.
    reduce_only: bool
    #: Idempotency key. The exchange rejects a retry with the same key
    #: as a duplicate instead of creating a second position.
    link_id: str


@dataclass(frozen=True)
class OrderPlan:
    """The full set of orders for one position."""

    symbol: str
    is_long: bool
    entry: Order
    stop_loss: Order
    #: None — time exit only: there is no target order.
    take_profit: Order | None
    #: Actual levels after rounding. They differ from the ones set by
    #: the signal, and further computations must use them.
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    qty: Decimal
    notional: Decimal

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(o for o in (self.entry, self.stop_loss, self.take_profit) if o)

    @property
    def risk_per_unit(self) -> Decimal:
        return abs(self.entry_price - self.stop_price)


@dataclass(frozen=True)
class PlanRejected:
    """The plan was not built. The reason is a code, not text (P-001)."""

    rejection: RejectionCode
    note: str


def round_to_tick(price: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    """Rounds a price to the instrument tick in the given direction."""
    if tick <= 0:
        raise ValueError(f"price tick must be positive: {tick}")
    steps = price / tick
    rounded = steps.to_integral_value(rounding="ROUND_CEILING" if up else "ROUND_FLOOR")
    return rounded * tick


def round_qty_down(qty: Decimal, step: Decimal) -> Decimal:
    """Rounds the quantity down. Only down: up would increase the
    position beyond the computed one, i.e. breach the loss limit."""
    if step <= 0:
        raise ValueError(f"lot step must be positive: {step}")
    return (qty / step).to_integral_value(rounding="ROUND_DOWN") * step


def link_id(symbol: str, moment: datetime, kind: OrderKind) -> str:
    """Deterministic idempotency key.

    Computed from the symbol, the decision moment and the order role — and nothing
    else. A rerun after a lost connection must produce THE SAME
    key, otherwise the exchange accepts the second order as new and opens a second
    position on top of the first.

    Randomness or the current time would be a bug here precisely because
    they look more harmless: everything works until the connection drops.
    """
    raw = f"{symbol}|{moment.isoformat()}|{kind.value}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
    return f"cb-{digest}"


def build(
    signal: Signal,
    *,
    spec: InstrumentSpec,
    qty: Decimal,
    moment: datetime,
) -> OrderPlan | PlanRejected:
    """An order plan or a rejection with a reason code.

    `qty` comes from the Risk Manager already computed: this module
    does not decide how much to take — it decides how to lay it onto the exchange
    grid without making the position larger or the risk wider.
    """
    try:
        return _build(signal, spec=spec, qty=qty, moment=moment)
    except (InvalidOperation, ZeroDivisionError, OverflowError, ValueError) as exc:
        return PlanRejected(
            RejectionCode.EXCHANGE_ERROR,
            f"arithmetic failed: {type(exc).__name__}: {exc}",
        )


def _build(
    signal: Signal, *, spec: InstrumentSpec, qty: Decimal, moment: datetime
) -> OrderPlan | PlanRejected:
    long = signal.is_long
    tick = spec.tick_size


    # The entry price is on the grid and AGAINST us: a long buys higher, a short
    # sells lower. The entry is market, and assuming the lucky side
    # of the tick is the same free profit as slippage in our
    # favour.
    entry_price = round_to_tick(signal.entry, tick, up=long)
    if entry_price <= 0:
        return PlanRejected(RejectionCode.EXCHANGE_ERROR, "entry price not positive")

    # Exits are computed from DISTANCES, not by rounding the levels
    # separately. Two independently rounded levels can
    # drift apart by a tick, and then the actual risk turns out WIDER
    # than specified — i.e. the limit the Risk Manager has just
    # computed is silently breached. Via distances the property is
    # provable: both prices are built from the already rounded entry.
    planned_risk = abs(signal.entry - signal.stop)
    # Time exit: no target, and the distance to it is undefined.
    planned_reward = (
        abs(signal.target - signal.entry) if signal.target is not None else None
    )
    if planned_risk <= 0 or (planned_reward is not None and planned_reward <= 0):
        return PlanRejected(
            RejectionCode.LIQ_BUFFER, "degenerate signal: zero distance"
        )

    # The stop goes CLOSER to the entry than specified: the loss will not exceed the plan.
    # The target goes FARTHER: it will not become more reachable than in the backtest.
    if long:
        stop_price = round_to_tick(entry_price - planned_risk, tick, up=True)
        target_price = (
            round_to_tick(entry_price + planned_reward, tick, up=True)
            if planned_reward is not None
            else None
        )
    else:
        stop_price = round_to_tick(entry_price + planned_risk, tick, up=False)
        target_price = (
            round_to_tick(entry_price - planned_reward, tick, up=False)
            if planned_reward is not None
            else None
        )

    if stop_price <= 0 or (target_price is not None and target_price <= 0):
        return PlanRejected(RejectionCode.EXCHANGE_ERROR, "level not positive")

    # Rounding could collapse the stop onto the entry price — on an instrument
    # with a coarse tick this is a real case, not paranoia.
    if (long and stop_price >= entry_price) or (not long and stop_price <= entry_price):
        return PlanRejected(
            RejectionCode.LIQ_BUFFER,
            f"after rounding to tick {spec.tick_size} the stop equals the entry price",
        )
    if target_price is not None and (
        (long and target_price <= entry_price) or (not long and target_price >= entry_price)
    ):
        return PlanRejected(
            RejectionCode.MIN_NOTIONAL,
            f"after rounding to tick {spec.tick_size} the target equals the entry price",
        )

    size = round_qty_down(qty, spec.qty_step)
    if size < spec.min_order_qty:
        return PlanRejected(
            RejectionCode.MIN_NOTIONAL,
            f"quantity {size} below the minimum lot {spec.min_order_qty}",
        )
    if spec.max_order_qty > 0 and size > spec.max_order_qty:
        return PlanRejected(
            RejectionCode.MIN_NOTIONAL,
            f"quantity {size} above the maximum lot {spec.max_order_qty}",
        )

    notional = size * entry_price
    if notional < spec.min_notional:
        return PlanRejected(
            RejectionCode.MIN_NOTIONAL,
            f"notional {notional} below the minimum {spec.min_notional}",
        )

    def order(kind: OrderKind, *, buy: bool, price: Decimal | None, reduce_only: bool):
        return Order(
            kind=kind,
            symbol=spec.symbol,
            buy=buy,
            qty=size,
            price=price,
            reduce_only=reduce_only,
            link_id=link_id(spec.symbol, moment, kind),
        )

    return OrderPlan(
        symbol=spec.symbol,
        is_long=long,
        entry=order(OrderKind.ENTRY, buy=long, price=None, reduce_only=False),
        # Exits close the position, so the side is opposite to the entry
        # and reduce_only is mandatory: without it a partial stop fill
        # on an already closed position would open an opposite one.
        stop_loss=order(
            OrderKind.STOP_LOSS, buy=not long, price=stop_price, reduce_only=True
        ),
        take_profit=(
            order(OrderKind.TAKE_PROFIT, buy=not long, price=target_price, reduce_only=True)
            if target_price is not None
            else None
        ),
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        qty=size,
        notional=notional,
    )


def time_close(plan: OrderPlan, moment: datetime) -> Order:
    """Close when the holding window expires. `moment` is the decision moment
    of the close: its idempotency key is its own, different from the entry key."""
    return Order(
        kind=OrderKind.TIME_CLOSE,
        symbol=plan.symbol,
        buy=not plan.is_long,
        qty=plan.qty,
        price=None,
        reduce_only=True,
        link_id=link_id(plan.symbol, moment, OrderKind.TIME_CLOSE),
    )


def emergency_close(plan: OrderPlan, moment: datetime) -> Order:
    """A market reduce-only order that closes the whole position."""
    return Order(
        kind=OrderKind.EMERGENCY_CLOSE,
        symbol=plan.symbol,
        buy=not plan.is_long,
        qty=plan.qty,
        price=None,
        reduce_only=True,
        link_id=link_id(plan.symbol, moment, OrderKind.EMERGENCY_CLOSE),
    )


__all__ = [
    "InstrumentSpec",
    "emergency_close",
    "time_close",
    "Order",
    "OrderKind",
    "OrderPlan",
    "PlanRejected",
    "build",
    "link_id",
    "round_qty_down",
    "round_to_tick",
]
