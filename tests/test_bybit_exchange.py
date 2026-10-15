"""Bybit testnet adapter. Phase 5, DEC-041.

The real exchange is unavailable without keys, so the translation is checked:
how Bybit responses become executor concepts and how executor
orders become request bodies. The response format follows the
v5 documentation; the first run on testnet will confirm or refute it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from src.backtest.engine import Signal
from src.core.config import load_settings
from src.exchange.trading_client import BybitTradingClient
from src.execution.bybit_exchange import (
    FALLS_TO_TRIGGER,
    RISES_TO_TRIGGER,
    BybitExchange,
    order_body,
    plain,
)
from src.execution.exchange import DuplicateOrder, LostResponse, RejectedOrder
from src.execution.plan import InstrumentSpec, OrderPlan, build

D = Decimal
TESTNET = "https://api-testnet.bybit.com"
T0 = datetime(2026, 5, 1, tzinfo=UTC)
SPEC = InstrumentSpec("SOLUSDT", D("0.01"), D("0.1"), D("0.1"), D("10000"), D("5"))


def plan(is_long: bool = True) -> OrderPlan:
    signal = (
        Signal(is_long=True, entry=D("100"), stop=D("90"), target=None)
        if is_long
        else Signal(is_long=False, entry=D("100"), stop=D("110"), target=None)
    )
    built = build(signal, spec=SPEC, qty=D("1"), moment=T0)
    assert isinstance(built, OrderPlan)
    return built


def exchange() -> BybitExchange:
    return BybitExchange(
        BybitTradingClient(
            load_settings().bybit, api_key=SecretStr("k"), api_secret=SecretStr("s")
        )
    )


def reply(code: int = 0, result: dict | None = None, msg: str = "OK") -> httpx.Response:
    return httpx.Response(200, json={"retCode": code, "retMsg": msg, "result": result or {}})


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------
def test_long_stop_triggers_on_a_fall():
    """A mixed-up direction is a stop that fires immediately or never."""
    body = order_body(plan(True).stop_loss, "linear")
    assert body["side"] == "Sell"
    assert body["triggerDirection"] == FALLS_TO_TRIGGER
    assert body["reduceOnly"] is True
    assert body["orderType"] == "Market"
    assert body["triggerBy"] == "LastPrice"


def test_short_stop_triggers_on_a_rise():
    body = order_body(plan(False).stop_loss, "linear")
    assert body["side"] == "Buy"
    assert body["triggerDirection"] == RISES_TO_TRIGGER


def test_entry_is_a_market_order_that_may_open():
    body = order_body(plan().entry, "linear")
    assert body["orderType"] == "Market"
    assert body["reduceOnly"] is False
    assert "triggerPrice" not in body


def test_numbers_are_sent_without_exponent():
    assert plain(D("0.0000001")) == "0.0000001"
    assert plain(D("100.500")) == "100.5"
    assert plain(D("1E+2")) == "100"


# ---------------------------------------------------------------------------
# Exchange responses → executor concepts
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@respx.mock
async def test_duplicate_link_id_means_the_first_attempt_went_through():
    respx.post(f"{TESTNET}/v5/order/create").mock(
        return_value=reply(110072, msg="OrderLinkedID is duplicate")
    )
    with pytest.raises(DuplicateOrder):
        await exchange().place(plan().entry)


@pytest.mark.asyncio
@respx.mock
async def test_network_failure_on_create_is_a_lost_response():
    respx.post(f"{TESTNET}/v5/order/create").mock(side_effect=httpx.ReadTimeout("t"))
    with pytest.raises(LostResponse):
        await exchange().place(plan().entry)


@pytest.mark.asyncio
@respx.mock
async def test_other_refusals_are_rejections():
    respx.post(f"{TESTNET}/v5/order/create").mock(
        return_value=reply(110007, msg="ab not enough for new order")
    )
    with pytest.raises(RejectedOrder):
        await exchange().place(plan().entry)


@pytest.mark.asyncio
@respx.mock
async def test_positions_are_parsed_and_empty_ones_skipped():
    respx.get(f"{TESTNET}/v5/position/list").mock(return_value=reply(result={
        "list": [
            {"symbol": "SOLUSDT", "side": "Buy", "size": "1.5", "avgPrice": "101.2"},
            {"symbol": "ETHUSDT", "side": "", "size": "0", "avgPrice": "0"},
            {"symbol": "ADAUSDT", "side": "Sell", "size": "30", "avgPrice": "0.5"},
        ],
        "nextPageCursor": "",
    }))
    got = await exchange().positions()
    assert [(p.symbol, p.is_long, p.qty) for p in got] == [
        ("ADAUSDT", False, D("30")),
        ("SOLUSDT", True, D("1.5")),
    ]


@pytest.mark.asyncio
@respx.mock
async def test_unknown_position_side_is_refused():
    """P-001: a position with an unclear side — do not guess."""
    respx.get(f"{TESTNET}/v5/position/list").mock(return_value=reply(result={
        "list": [{"symbol": "SOLUSDT", "side": "Both", "size": "1", "avgPrice": "1"}],
    }))
    with pytest.raises(ValueError, match="unknown side"):
        await exchange().positions()


@pytest.mark.asyncio
@respx.mock
async def test_open_orders_include_untriggered_conditional_stops():
    """Without the second request the stop might not appear in the listing, and reconciliation
    would decide the position is unprotected."""
    route = respx.get(f"{TESTNET}/v5/order/realtime")
    route.side_effect = [
        reply(result={"list": [], "nextPageCursor": ""}),
        reply(result={"list": [{"orderLinkId": "cb-stop", "symbol": "SOLUSDT",
                                "qty": "1", "reduceOnly": True}]}),
    ]
    got = await exchange().open_orders()
    assert [o.link_id for o in got] == ["cb-stop"]
    assert "orderFilter=StopOrder" in str(route.calls[1].request.url)


@pytest.mark.asyncio
@respx.mock
async def test_cancel_of_a_missing_order_is_not_an_error():
    respx.post(f"{TESTNET}/v5/order/create").mock(return_value=reply(result={"orderId": "1"}))
    respx.post(f"{TESTNET}/v5/order/cancel").mock(
        return_value=reply(110001, msg="order not exists or too late to cancel")
    )
    ex = exchange()
    stop = plan().stop_loss
    await ex.place(stop)
    await ex.cancel(stop.link_id)


@pytest.mark.asyncio
@respx.mock
async def test_cancel_of_an_unknown_key_looks_it_up_first():
    respx.get(f"{TESTNET}/v5/order/realtime").mock(return_value=reply(result={"list": []}))
    cancel = respx.post(f"{TESTNET}/v5/order/cancel")
    await exchange().cancel("no-such-key")
    assert cancel.call_count == 0


@respx.mock
async def test_filled_order_reports_price_qty_and_fee():
    respx.get(f"{TESTNET}/v5/order/history").mock(return_value=reply(result={"list": [
        {"orderLinkId": "cb-1", "orderStatus": "Filled", "avgPrice": "101.5",
         "cumExecQty": "2", "cumExecFee": "0.11"},
    ]}))
    fill = await exchange().fill("cb-1", "SOLUSDT")
    assert fill is not None
    assert (fill.avg_price, fill.qty, fill.fee) == (D("101.5"), D("2"), D("0.11"))


@respx.mock
async def test_untriggered_stop_is_not_a_fill():
    respx.get(f"{TESTNET}/v5/order/history").mock(return_value=reply(result={"list": [
        {"orderLinkId": "cb-1", "orderStatus": "Untriggered", "avgPrice": "",
         "cumExecQty": "0", "cumExecFee": "0"},
    ]}))
    assert await exchange().fill("cb-1", "SOLUSDT") is None


@respx.mock
async def test_fill_falls_back_to_realtime_when_history_is_behind():
    respx.get(f"{TESTNET}/v5/order/history").mock(return_value=reply(result={"list": []}))
    respx.get(f"{TESTNET}/v5/order/realtime").mock(return_value=reply(result={"list": [
        {"orderLinkId": "cb-1", "orderStatus": "Filled", "avgPrice": "99",
         "cumExecQty": "1", "cumExecFee": "0.05"},
    ]}))
    fill = await exchange().fill("cb-1", "SOLUSDT")
    assert fill is not None and fill.avg_price == D("99")


@respx.mock
async def test_instrument_is_read_from_the_executing_venue():
    respx.get(f"{TESTNET}/v5/market/instruments-info").mock(return_value=reply(result={"list": [
        {"symbol": "SOLUSDT", "status": "Trading", "priceFilter": {"tickSize": "0.01"},
         "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "maxOrderQty": "8000",
                           "maxMktOrderQty": "500", "minNotionalValue": "5"}},
    ]}))
    spec = await exchange().instrument("SOLUSDT")
    # The ceiling is market-based: the entry and exits use market orders.
    assert spec == InstrumentSpec("SOLUSDT", D("0.01"), D("0.1"), D("0.1"), D("500"), D("5"))


@respx.mock
async def test_instrument_not_trading_is_unknown():
    respx.get(f"{TESTNET}/v5/market/instruments-info").mock(return_value=reply(result={"list": [
        {"symbol": "SOLUSDT", "status": "Settling", "priceFilter": {"tickSize": "0.01"},
         "lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1"}},
    ]}))
    assert await exchange().instrument("SOLUSDT") is None


@respx.mock
async def test_last_price_is_parsed_and_zero_is_unknown():
    route = respx.get(f"{TESTNET}/v5/market/tickers")
    route.mock(return_value=reply(result={"list": [{"symbol": "SOLUSDT", "lastPrice": "142.3"}]}))
    assert await exchange().last_price("SOLUSDT") == D("142.3")
    route.mock(return_value=reply(result={"list": [{"symbol": "SOLUSDT", "lastPrice": "0"}]}))
    assert await exchange().last_price("SOLUSDT") is None
