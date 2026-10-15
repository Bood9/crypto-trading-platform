"""The client's exception boundary.

Contract: only BybitError and its subclasses leave BybitClient.
The single exception is asyncio.CancelledError, which
since Python 3.8 derives from BaseException: it must not be swallowed, it is
flow control, not a failure.

The contract is not cosmetic. Collectors catch BybitError and use it to decide
whether to record a single symbol's rejection or crash entirely. Any foreign type
bypasses that branch and brings down the whole backfill — exactly how a raw
httpx.TimeoutException once behaved.

Checked layer by layer: at each level a foreign exception is injected,
and the test asserts it is wrapped at the boundary.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from src.core.config import load_settings
from src.exchange.bybit_client import (
    BybitClient,
    BybitError,
    BybitNetworkError,
)
from src.exchange.rate_limiter import TokenBucket

BASE = "https://api.bybit.com"


class NoSleep:
    async def __call__(self, seconds: float) -> None:
        return None


class FixedBackoff:
    def delay(self, attempt: int) -> float:  # noqa: ARG002
        return 0.0


@pytest.fixture
def config():
    return load_settings().bybit


def make_client(config, **kwargs) -> BybitClient:
    return BybitClient(
        config,
        bucket=TokenBucket(rate=10_000, burst=10_000),
        network_backoff=FixedBackoff(),
        rate_limit_backoff=FixedBackoff(),
        sleep=NoSleep(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Transport layer
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("timeout"),
        httpx.ConnectError("dns"),
        httpx.ReadError("reset"),
        httpx.PoolTimeout("pool"),
        httpx.ProtocolError("bad protocol"),
        # These two are RequestError but NOT TransportError: they used to bypass
        # the handler.
        httpx.DecodingError("bad encoding"),
        httpx.TooManyRedirects("loop"),
    ],
)
async def test_httpx_request_errors_are_wrapped(config, exc):
    respx.get(f"{BASE}/v5/market/tickers").mock(side_effect=exc)

    async with make_client(config) as client:
        with pytest.raises(BybitNetworkError):
            await client.get_tickers()


@respx.mock
@pytest.mark.parametrize(
    "exc",
    [
        # Not even part of HTTPError.
        httpx.InvalidURL("bad url"),
        httpx.StreamError("stream"),
        # Entirely foreign.
        RuntimeError("something went wrong"),
        MemoryError("out of memory"),
        OSError("socket"),
        TimeoutError("async timeout"),
    ],
)
async def test_foreign_transport_exceptions_are_wrapped(config, exc):
    respx.get(f"{BASE}/v5/market/tickers").mock(side_effect=exc)

    async with make_client(config) as client:
        with pytest.raises(BybitError) as raised:
            await client.get_tickers()

    assert type(raised.value).__module__.startswith("src.")


class RaisingHttpClient:
    """A fake httpx client. respx cannot return CancelledError
    as a side effect — it turns it into its own AssertionError,
    so this path is checked bypassing it."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    async def get(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        raise self.exc

    async def aclose(self) -> None:
        return None


async def test_cancellation_is_not_swallowed(config):
    """CancelledError is flow control, not a failure.

    By swallowing it the client would break graceful shutdown (spec §13):
    the task would keep retrying after a cancellation request.

    This works because since Python 3.8 CancelledError
    derives from BaseException and is not caught by `except Exception`.
    """
    transport = RaisingHttpClient(asyncio.CancelledError())
    client = make_client(config, client=transport)

    with pytest.raises(asyncio.CancelledError):
        await client.get_tickers()

    # Cancellation must not be retried.
    assert transport.calls == 1


async def test_keyboard_interrupt_is_not_swallowed(config):
    """Same for KeyboardInterrupt: SIGINT must stop the process."""
    transport = RaisingHttpClient(KeyboardInterrupt())
    client = make_client(config, client=transport)

    with pytest.raises(KeyboardInterrupt):
        await client.get_tickers()

    assert transport.calls == 1


# ---------------------------------------------------------------------------
# Response parsing layer
# ---------------------------------------------------------------------------
@respx.mock
async def test_non_json_body_is_wrapped(config):
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(200, text="<html>maintenance</html>")
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError):
            await client.get_tickers()


@respx.mock
async def test_json_array_instead_of_object_is_wrapped(config):
    """Calling .get() on a list would raise AttributeError."""
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError) as raised:
            await client.get_tickers()

    assert "JSON object" in str(raised.value)


@respx.mock
async def test_json_scalar_is_wrapped(config):
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(200, json="just a string")
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError):
            await client.get_tickers()


@respx.mock
async def test_result_not_an_object_is_wrapped(config):
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(
            200, json={"retCode": 0, "retMsg": "OK", "result": ["not an object"]}
        )
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError) as raised:
            await client.get_tickers()

    assert "result" in str(raised.value)


@respx.mock
async def test_non_numeric_ret_code_is_wrapped(config):
    """int() on garbage would raise ValueError past all handlers."""
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(
            200, json={"retCode": "not a number", "retMsg": "?", "result": {}}
        )
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError) as raised:
            await client.get_tickers()

    assert "retCode" in str(raised.value)


@respx.mock
async def test_missing_ret_code_is_wrapped(config):
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(200, json={"result": {}})
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError):
            await client.get_tickers()


# ---------------------------------------------------------------------------
# End-to-end check across all public methods
# ---------------------------------------------------------------------------
@respx.mock
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_tickers(),
        lambda c: c.get_instruments(),
        lambda c: c.get_risk_limit("SOLUSDT"),
        lambda c: c.get_kline("SOLUSDT", "60"),
        lambda c: c.get_funding_history("SOLUSDT"),
        lambda c: c.get_open_interest("SOLUSDT"),
        lambda c: c.get_delisting_announcements(),
    ],
)
async def test_every_public_method_wraps_foreign_exceptions(config, call):
    """No method may let a foreign type escape."""
    respx.route(host="api.bybit.com").mock(side_effect=RuntimeError("surprise"))

    async with make_client(config) as client:
        with pytest.raises(BybitError):
            await call(client)


@respx.mock
@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.get_tickers(),
        lambda c: c.get_instruments(),
        lambda c: c.get_kline("SOLUSDT", "60"),
        lambda c: c.get_open_interest("SOLUSDT"),
    ],
)
async def test_every_public_method_wraps_broken_json(config, call):
    respx.route(host="api.bybit.com").mock(
        return_value=httpx.Response(200, json=["array instead of an object"])
    )

    async with make_client(config) as client:
        with pytest.raises(BybitError):
            await call(client)
