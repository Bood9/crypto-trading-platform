"""Bybit trading client. Phase 5.

Three properties where a mistake costs money: the signature, the ban on the main
exchange, and that order creation is not retried by the client itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from src.core.config import load_settings
from src.exchange.trading_client import (
    RECV_WINDOW_MS,
    BybitTradingClient,
    TradingAPIError,
    TradingNetworkError,
    assert_testnet,
    sign,
)

TESTNET = "https://api-testnet.bybit.com"
KEY, SECRET = "k3y", "s3cret"
NOW = 1_700_000_000.0


def client(**kw) -> BybitTradingClient:
    return BybitTradingClient(
        load_settings().bybit,
        api_key=SecretStr(KEY),
        api_secret=SecretStr(SECRET),
        clock=lambda: NOW,
        **kw,
    )


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------
def test_signature_follows_the_documented_order():
    """timestamp + key + recvWindow + payload, HMAC-SHA256 in hex."""
    expected = hmac.new(
        SECRET.encode(), f"1700000000000{KEY}5000category=linear".encode(), hashlib.sha256
    ).hexdigest()
    assert sign(SECRET, 1_700_000_000_000, KEY, 5000, "category=linear") == expected


@pytest.mark.asyncio
@respx.mock
async def test_get_signs_exactly_the_query_it_sends():
    route = respx.get(f"{TESTNET}/v5/position/list").mock(return_value=ok({"list": []}))
    await client().get("/v5/position/list", {"settleCoin": "USDT", "category": "linear"})

    request = route.calls.last.request
    sent_query = request.url.query.decode()
    assert sent_query == "category=linear&settleCoin=USDT"
    assert request.headers["X-BAPI-SIGN"] == sign(
        SECRET, int(NOW * 1000), KEY, RECV_WINDOW_MS, sent_query
    )


@pytest.mark.asyncio
@respx.mock
async def test_post_signs_exactly_the_body_it_sends():
    route = respx.post(f"{TESTNET}/v5/order/create").mock(return_value=ok({"orderId": "1"}))
    await client().post("/v5/order/create", {"symbol": "SOLUSDT", "qty": "1"})

    request = route.calls.last.request
    body = request.content.decode()
    assert json.loads(body) == {"qty": "1", "symbol": "SOLUSDT"}
    assert request.headers["X-BAPI-SIGN"] == sign(
        SECRET, int(NOW * 1000), KEY, RECV_WINDOW_MS, body
    )


# ---------------------------------------------------------------------------
# Testnet only
# ---------------------------------------------------------------------------
def test_mainnet_address_is_refused():
    with pytest.raises(ValueError, match="only with testnet"):
        assert_testnet("https://api.bybit.com")
    assert assert_testnet(TESTNET) == TESTNET


def test_client_refuses_to_exist_if_testnet_address_points_to_mainnet():
    """Swapping the testnet address in the config must not turn paper
    trading into real trading."""
    config = load_settings().bybit.model_copy(update={"testnet_base_url": "https://api.bybit.com"})
    with pytest.raises(ValueError, match="only with testnet"):
        BybitTradingClient(config, api_key=SecretStr(KEY), api_secret=SecretStr(SECRET))


def test_client_has_no_way_to_select_mainnet():
    """Neither a parameter nor a flag: the main exchange is separate code, not a setting."""
    import inspect

    params = inspect.signature(BybitTradingClient.__init__).parameters
    assert "testnet" not in params
    assert "base_url" not in params


def test_missing_keys_are_refused():
    """P-001: no key means rejection, not "let's try without a signature"."""
    with pytest.raises(ValueError, match="no testnet key"):
        BybitTradingClient(
            load_settings().bybit, api_key=SecretStr(""), api_secret=SecretStr("")
        )


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@respx.mock
async def test_order_creation_is_sent_exactly_once_on_timeout():
    """Retrying a POST after a timeout is a way to open a second position."""
    route = respx.post(f"{TESTNET}/v5/order/create").mock(
        side_effect=httpx.ReadTimeout("timeout")
    )
    with pytest.raises(TradingNetworkError):
        await client().post("/v5/order/create", {"symbol": "SOLUSDT"})
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_reads_are_retried():
    route = respx.get(f"{TESTNET}/v5/position/list")
    route.side_effect = [httpx.ConnectError("no"), httpx.Response(502), ok({"list": [1]})]
    result = await client().get("/v5/position/list", {"category": "linear"})
    assert result == {"list": [1]}
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_api_rejection_carries_the_code():
    respx.post(f"{TESTNET}/v5/order/create").mock(
        return_value=httpx.Response(
            200, json={"retCode": 110072, "retMsg": "OrderLinkedID is duplicate", "result": {}}
        )
    )
    with pytest.raises(TradingAPIError) as info:
        await client().post("/v5/order/create", {})
    assert info.value.ret_code == 110072
