"""Bybit testnet exchange behind the Executor interface. Phase 5, DEC-041.

The Executor is tested on a paper exchange that breaks like a real one
(DEC-027). This module maps the real exchange onto the same concepts:

* a retry with an already seen `orderLinkId` — Bybit answers retCode 110072;
  for the Executor this is `DuplicateOrder`, i.e. a CONFIRMATION that
  the first attempt went through;
* a network failure while creating an order — `LostResponse`: the order may
  have filled, and only the exchange knows;
* any other rejection — `RejectedOrder`.

A stop is a conditional market order: to close a long (sell) it
triggers when the price FALLS, to close a short (buy) — when it rises.
A swapped direction would turn the stop into an order that triggers
immediately or never — so it is derived from the order side by a single
function and covered by a test in both directions.

The trigger price is the last trade price (`LastPrice`): that is how the
backtest computes it, checking the stop against the bar's high and low.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.core.logging import get_logger
from src.exchange.trading_client import (
    BybitTradingClient,
    TradingAPIError,
    TradingNetworkError,
)
from src.execution.exchange import DuplicateOrder, Fill, LostResponse, RejectedOrder
from src.execution.plan import InstrumentSpec, Order, OrderKind
from src.execution.reconcile import ExchangeOrder, ExchangePosition

log = get_logger(__name__)

#: Bybit: «OrderLinkedID is duplicate».
DUPLICATE_LINK_ID = 110072
#: Bybit: «order not exists or too late to cancel».
ORDER_NOT_FOUND = 110001

#: Bybit trigger direction for a conditional order.
RISES_TO_TRIGGER = 1
FALLS_TO_TRIGGER = 2

#: Statuses at which the order has a completed fill.
#: Partially filled with the remainder cancelled is also a fill: the position
#: changed by exactly the filled quantity, and it must be accounted for.
FINISHED_WITH_FILL = {"Filled", "PartiallyFilledCanceled"}

MARKET_KINDS = {OrderKind.ENTRY, OrderKind.TIME_CLOSE, OrderKind.EMERGENCY_CLOSE}


def plain(value: Decimal) -> str:
    """A number without an exponent: `str(Decimal)` gives '1E-7', the exchange rejects it."""
    text = format(value.normalize(), "f")
    return text


def trigger_direction(order: Order) -> int:
    """A buy stop closes a short — triggers when the price rises;
    a sell stop closes a long — when it falls."""
    return RISES_TO_TRIGGER if order.buy else FALLS_TO_TRIGGER


def order_body(order: Order, category: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "category": category,
        "symbol": order.symbol,
        "side": "Buy" if order.buy else "Sell",
        "qty": plain(order.qty),
        "orderLinkId": order.link_id,
        "positionIdx": 0,  # one-way position mode
        "reduceOnly": order.reduce_only,
    }
    if order.kind is OrderKind.STOP_LOSS:
        if order.price is None:
            raise ValueError("stop without a trigger price")
        body |= {
            "orderType": "Market",
            "triggerPrice": plain(order.price),
            "triggerDirection": trigger_direction(order),
            "triggerBy": "LastPrice",
        }
    elif order.kind is OrderKind.TAKE_PROFIT:
        if order.price is None:
            raise ValueError("target without a price")
        body |= {"orderType": "Limit", "price": plain(order.price), "timeInForce": "GTC"}
    elif order.kind in MARKET_KINDS:
        body["orderType"] = "Market"
    else:  # P-001: an unknown order role — never "send it as market".
        raise ValueError(f"unknown order role {order.kind}")
    return body


class BybitExchange:
    """`Exchange` on top of the testnet trading client."""

    def __init__(self, client: BybitTradingClient, settle_coin: str = "USDT") -> None:
        self._client = client
        self._settle = settle_coin
        #: link_id → symbol: Bybit cancellation needs the symbol, while the Executor
        #: knows only the key.
        self._symbol_of: dict[str, str] = {}

    async def place(self, order: Order) -> None:
        body = order_body(order, self._client.category)
        self._symbol_of[order.link_id] = order.symbol
        try:
            await self._client.post("/v5/order/create", body)
        except TradingNetworkError as exc:
            raise LostResponse(f"{order.link_id}: {exc}") from exc
        except TradingAPIError as exc:
            if exc.ret_code == DUPLICATE_LINK_ID:
                raise DuplicateOrder(order.link_id) from exc
            raise RejectedOrder(f"{order.link_id}: {exc}") from exc

    async def cancel(self, link_id: str) -> None:
        symbol = self._symbol_of.get(link_id)
        if symbol is None:
            await self.open_orders()  # fills the key map
            symbol = self._symbol_of.get(link_id)
        if symbol is None:
            return  # no such order on the exchange — nothing to cancel
        try:
            await self._client.post(
                "/v5/order/cancel",
                {"category": self._client.category, "symbol": symbol, "orderLinkId": link_id},
            )
        except TradingAPIError as exc:
            if exc.ret_code != ORDER_NOT_FOUND:
                log.warning("cancel.failed", link_id=link_id, error=str(exc))
        except TradingNetworkError as exc:
            # Do not raise: the caller checks the result against open
            # orders, and a stuck order will show up in reconciliation.
            log.warning("cancel.lost", link_id=link_id, error=str(exc))

    async def _paged(self, endpoint: str, params: dict[str, Any]) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(50):  # guard against an endless cursor
            page = await self._client.get(endpoint, params | ({"cursor": cursor} if cursor else {}))
            out.extend(page.get("list") or [])
            cursor = page.get("nextPageCursor") or None
            if not cursor:
                break
        return out

    async def positions(self) -> list[ExchangePosition]:
        rows = await self._paged(
            "/v5/position/list",
            {"category": self._client.category, "settleCoin": self._settle, "limit": 200},
        )
        out = []
        for row in rows:
            size = Decimal(str(row.get("size") or "0"))
            if size <= 0:
                continue
            side = row.get("side")
            if side not in {"Buy", "Sell"}:
                # P-001: a position with an unclear side — do not guess.
                raise ValueError(f"position {row.get('symbol')} with unknown side {side!r}")
            out.append(
                ExchangePosition(
                    symbol=row["symbol"],
                    is_long=side == "Buy",
                    qty=size,
                    entry_price=Decimal(str(row.get("avgPrice") or "0")),
                )
            )
        return sorted(out, key=lambda p: p.symbol)

    async def open_orders(self) -> list[ExchangeOrder]:
        """Active and NOT YET TRIGGERED CONDITIONAL orders.

        Two requests merged by key: without the filter, conditional orders
        (and a stop is conditional) may be missing from the output, and reconciliation
        would decide the position is unprotected.
        """
        base = {"category": self._client.category, "settleCoin": self._settle, "limit": 50}
        rows = await self._paged("/v5/order/realtime", base)
        rows += await self._paged("/v5/order/realtime", base | {"orderFilter": "StopOrder"})
        seen: dict[str, ExchangeOrder] = {}
        for row in rows:
            link = row.get("orderLinkId") or ""
            if not link or link in seen:
                continue
            self._symbol_of[link] = row["symbol"]
            seen[link] = ExchangeOrder(
                link_id=link,
                symbol=row["symbol"],
                qty=Decimal(str(row.get("qty") or "0")),
                reduce_only=bool(row.get("reduceOnly")),
            )
        return sorted(seen.values(), key=lambda o: o.link_id)

    async def fill(self, link_id: str, symbol: str) -> Fill | None:
        """Fill of an order by key.

        History first, then current orders: a just-filled order may not
        be in the history yet, while an old one may have dropped out of current orders.
        """
        params = {"category": self._client.category, "symbol": symbol, "orderLinkId": link_id}
        for endpoint in ("/v5/order/history", "/v5/order/realtime"):
            rows = (await self._client.get(endpoint, params)).get("list") or []
            for row in rows:
                if row.get("orderLinkId") != link_id:
                    continue
                if row.get("orderStatus") not in FINISHED_WITH_FILL:
                    return None
                qty = Decimal(str(row.get("cumExecQty") or "0"))
                price = Decimal(str(row.get("avgPrice") or "0"))
                if qty <= 0 or price <= 0:
                    return None
                return Fill(
                    avg_price=price,
                    qty=qty,
                    fee=Decimal(str(row.get("cumExecFee") or "0")),
                )
        return None

    async def last_price(self, symbol: str) -> Decimal | None:
        rows = (
            await self._client.get(
                "/v5/market/tickers", {"category": self._client.category, "symbol": symbol}
            )
        ).get("list") or []
        for row in rows:
            if row.get("symbol") == symbol and row.get("lastPrice"):
                price = Decimal(str(row["lastPrice"]))
                return price if price > 0 else None
        return None

    async def instrument(self, symbol: str) -> InstrumentSpec | None:
        """Instrument parameters of THIS exchange. Not trading — None (P-001)."""
        rows = (
            await self._client.get(
                "/v5/market/instruments-info",
                {"category": self._client.category, "symbol": symbol},
            )
        ).get("list") or []
        for row in rows:
            if row.get("symbol") != symbol or row.get("status") != "Trading":
                continue
            price_filter = row.get("priceFilter") or {}
            lot = row.get("lotSizeFilter") or {}
            try:
                return InstrumentSpec(
                    symbol=symbol,
                    tick_size=Decimal(str(price_filter["tickSize"])),
                    qty_step=Decimal(str(lot["qtyStep"])),
                    min_order_qty=Decimal(str(lot["minOrderQty"])),
                    # Entry and exits are market: the cap is the market one, lower than limit.
                    max_order_qty=Decimal(
                        str(lot.get("maxMktOrderQty") or lot.get("maxOrderQty") or "0")
                    ),
                    min_notional=Decimal(str(lot.get("minNotionalValue") or "0")),
                )
            except (KeyError, ArithmeticError):
                return None
        return None


__all__ = [
    "DUPLICATE_LINK_ID",
    "FINISHED_WITH_FILL",
    "FALLS_TO_TRIGGER",
    "ORDER_NOT_FOUND",
    "RISES_TO_TRIGGER",
    "BybitExchange",
    "order_body",
    "plain",
    "trigger_direction",
]
