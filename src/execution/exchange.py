"""Exchange interface for the Executor and a paper implementation. Spec §8.

The Executor must not know whether it trades on Bybit, on testnet or on
paper. Otherwise it could only be tested with real money —
i.e. nobody would test it at all.

The paper exchange here is not a stub but a model with the properties
that break executors:

* an order with an already seen `link_id` is rejected as a duplicate, not
  filled a second time — Bybit behaves this way, and the idempotency of
  retries after a lost connection relies on it;
* a reduce-only order can neither open nor increase a position;
* sending can be forced to fail AFTER the fill — the nastiest
  failure, when the order went through but the response was lost.

The last one matters most. An Executor tested only against an exchange
that always answers breaks exactly at the moment the response does not
arrive — and finds out from a second position.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from src.execution.plan import InstrumentSpec, Order
from src.execution.reconcile import ExchangeOrder, ExchangePosition


class DuplicateOrder(Exception):
    """An order with this `link_id` already existed. Not an Executor error but a signal
    that the previous attempt actually went through."""


class LostResponse(Exception):
    """The order filled, but the response never reached us."""


class RejectedOrder(Exception):
    """The exchange rejected on the merits: reduce-only without a position and the like."""


@dataclass(frozen=True)
class Fill:
    """An order fill as the exchange sees it: average price, quantity, fee.

    The trade result is computed from fills, not from planned
    prices: the planned price is what we counted on, while money
    is charged for what actually happened.
    """

    avg_price: Decimal
    qty: Decimal
    fee: Decimal


class Exchange(Protocol):
    """What the Executor needs from the exchange. Nothing more."""

    async def place(self, order: Order) -> None: ...

    async def cancel(self, link_id: str) -> None: ...

    async def positions(self) -> list[ExchangePosition]: ...

    async def open_orders(self) -> list[ExchangeOrder]: ...


class TradingVenue(Exchange, Protocol):
    """The exchange as the daily cycle sees it: the Executor plus what is needed
    for the decision and accounting.

    Price and instrument parameters are taken from THE exchange where the order
    is executed. Price and lot steps differ between testnet and the main exchange,
    and a plan laid onto someone else's grid will be rejected by the exchange — or,
    worse, accepted with a different risk.
    """

    async def fill(self, link_id: str, symbol: str) -> Fill | None:
        """The fill of the whole order; None — not filled (or unknown)."""
        ...

    async def last_price(self, symbol: str) -> Decimal | None: ...

    async def instrument(self, symbol: str) -> InstrumentSpec | None: ...


@dataclass
class PaperExchange:
    """A paper exchange with the rules that break executors.

    A market entry fills immediately at `mark`. Stop and target stay
    pending until they are cancelled or the price moves
    (`move_price`).
    """

    mark: dict[str, Decimal] = field(default_factory=dict)
    #: symbol → (long?, quantity, entry price)
    _positions: dict[str, tuple[bool, Decimal, Decimal]] = field(default_factory=dict)
    _resting: dict[str, Order] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)
    #: link_ids on which sending will "fill, but the response gets lost".
    lose_response_for: set[str] = field(default_factory=set)
    #: link_ids the exchange will reject on the merits.
    reject: set[str] = field(default_factory=set)
    log: list[str] = field(default_factory=list)
    #: Instrument parameters; no record — the instrument is not trading.
    specs: dict[str, InstrumentSpec] = field(default_factory=dict)
    #: Taker fee, % of notional.
    fee_pct: Decimal = Decimal("0.055")
    #: Slippage of market fills, % against us. Zero by
    #: default: Executor tests need an exact price, not a market model.
    slippage_pct: Decimal = Decimal(0)
    _fills: dict[str, Fill] = field(default_factory=dict)

    def _slipped(self, price: Decimal, *, buy: bool) -> Decimal:
        factor = self.slippage_pct / 100
        return price * (1 + factor) if buy else price * (1 - factor)

    def _record_fill(self, link_id: str, price: Decimal, qty: Decimal) -> Decimal:
        self._fills[link_id] = Fill(
            avg_price=price, qty=qty, fee=price * qty * self.fee_pct / 100
        )
        return price

    async def fill(self, link_id: str, symbol: str) -> Fill | None:
        del symbol
        return self._fills.get(link_id)

    async def last_price(self, symbol: str) -> Decimal | None:
        return self.mark.get(symbol)

    async def instrument(self, symbol: str) -> InstrumentSpec | None:
        return self.specs.get(symbol)

    async def place(self, order: Order) -> None:
        if order.link_id in self._seen:
            raise DuplicateOrder(order.link_id)
        if order.link_id in self.reject:
            raise RejectedOrder(order.link_id)

        if order.reduce_only:
            held = self._positions.get(order.symbol)
            if held is None:
                raise RejectedOrder(f"{order.link_id}: reduce-only without a position")
            long, held_qty, _entry = held
            # reduce-only must be opposite to the position.
            if order.buy == long:
                raise RejectedOrder(f"{order.link_id}: reduce-only in the position direction")
            self._seen.add(order.link_id)
            if order.price is None:
                # A market reduce-only is an immediate close, and all pending
                # exits go away together with the position.
                self._positions.pop(order.symbol, None)
                for other in [k for k, o in self._resting.items() if o.symbol == order.symbol]:
                    self._resting.pop(other, None)
                price = self.mark.get(order.symbol)
                if price is not None:
                    self._record_fill(
                        order.link_id, self._slipped(price, buy=order.buy), held_qty
                    )
                self.log.append(f"closed at market {order.symbol}")
            else:
                self._resting[order.link_id] = order
                self.log.append(f"placed {order.kind.value} {order.symbol}")
        else:
            price = self.mark.get(order.symbol)
            if price is None:
                raise RejectedOrder(f"{order.symbol}: no price")
            if order.symbol in self._positions:
                raise RejectedOrder(f"{order.symbol}: position already open")
            self._seen.add(order.link_id)
            filled = self._record_fill(
                order.link_id, self._slipped(price, buy=order.buy), order.qty
            )
            self._positions[order.symbol] = (order.buy, order.qty, filled)
            self.log.append(f"entry filled {order.symbol} at {price}")

        if order.link_id in self.lose_response_for:
            raise LostResponse(order.link_id)

    async def cancel(self, link_id: str) -> None:
        if self._resting.pop(link_id, None) is not None:
            self.log.append(f"cancelled {link_id}")

    async def positions(self) -> list[ExchangePosition]:
        return [
            ExchangePosition(symbol=s, is_long=long, qty=qty, entry_price=entry)
            for s, (long, qty, entry) in sorted(self._positions.items())
        ]

    async def open_orders(self) -> list[ExchangeOrder]:
        return [
            ExchangeOrder(
                link_id=o.link_id, symbol=o.symbol, qty=o.qty, reduce_only=o.reduce_only
            )
            for o in sorted(self._resting.values(), key=lambda o: o.link_id)
        ]

    def move_price(self, symbol: str, price: Decimal) -> None:
        """A price move. A touched stop or target closes the position, and the other
        exit is cancelled — like OCO on the exchange."""
        self.mark[symbol] = price
        held = self._positions.get(symbol)
        if held is None:
            return
        long, held_qty, _entry = held
        for link, order in sorted(self._resting.items()):
            if order.symbol != symbol or order.price is None:
                continue
            is_stop = order.kind.value == "STOP_LOSS"
            hit = (
                (price <= order.price if is_stop else price >= order.price)
                if long
                else (price >= order.price if is_stop else price <= order.price)
            )
            if hit:
                self._positions.pop(symbol, None)
                # The stop is market: fills at the current price, which on a gap
                # is worse than the trigger level. The target is a limit.
                self._record_fill(
                    link,
                    self._slipped(price, buy=order.buy) if is_stop else order.price,
                    held_qty,
                )
                self.log.append(f"triggered {order.kind.value} {symbol} at {price}")
                for other in [k for k, o in self._resting.items() if o.symbol == symbol]:
                    self._resting.pop(other, None)
                return


__all__ = [
    "DuplicateOrder",
    "Exchange",
    "Fill",
    "TradingVenue",
    "LostResponse",
    "PaperExchange",
    "RejectedOrder",
]
