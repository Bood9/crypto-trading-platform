"""Tests for the rate-limit circuit breaker.

A repeated 403 after several full cycles of five-minute pauses
is no longer a rate-limit window but an IP ban or a regional block. New
attempts only extend the block, so the collector must
stop instead of retrying forever.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from src.core.config import load_settings
from src.exchange.bybit_client import (
    BybitCircuitOpen,
    BybitClient,
    BybitError,
    BybitNetworkError,
)
from src.exchange.rate_limiter import TokenBucket

BASE = "https://api.bybit.com"


class NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class FixedBackoff:
    def __init__(self, value: float) -> None:
        self.value = value

    def delay(self, attempt: int) -> float:  # noqa: ARG002
        return self.value


@pytest.fixture
def config():
    return load_settings().bybit


@pytest.fixture
def sleeper():
    return NoSleep()


def make_client(config, sleeper) -> BybitClient:
    return BybitClient(
        config,
        bucket=TokenBucket(rate=10_000, burst=10_000),
        network_backoff=FixedBackoff(0.01),
        rate_limit_backoff=FixedBackoff(0.01),
        sleep=sleeper,
    )


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


@respx.mock
async def test_circuit_opens_after_configured_cycles(config, sleeper):
    respx.get(f"{BASE}/v5/market/tickers").mock(return_value=httpx.Response(403))
    limit = config.rate_limit.circuit_breaker.max_consecutive_rate_limit_failures

    async with make_client(config, sleeper) as client:
        # Cycles up to the last one are exhausted with a regular error.
        for _ in range(limit - 1):
            with pytest.raises(BybitError) as exc:
                await client.get_tickers()
            assert not isinstance(exc.value, BybitCircuitOpen)
            assert not client.circuit_open

        with pytest.raises(BybitCircuitOpen):
            await client.get_tickers()
        assert client.circuit_open


@respx.mock
async def test_open_circuit_rejects_further_calls_without_network(config, sleeper):
    """After tripping, requests must not reach the exchange."""
    route = respx.get(f"{BASE}/v5/market/tickers").mock(return_value=httpx.Response(429))
    limit = config.rate_limit.circuit_breaker.max_consecutive_rate_limit_failures

    async with make_client(config, sleeper) as client:
        for _ in range(limit):
            with pytest.raises(BybitError):
                await client.get_tickers()

        calls_before = route.call_count
        for _ in range(5):
            with pytest.raises(BybitCircuitOpen):
                await client.get_tickers()

        assert route.call_count == calls_before, "requests keep going to the exchange"


@respx.mock
async def test_success_resets_the_counter(config, sleeper):
    """CONSECUTIVE rejections are counted, not their total over the run."""
    limit = config.rate_limit.circuit_breaker.max_consecutive_rate_limit_failures
    route = respx.get(f"{BASE}/v5/market/tickers")

    # An exhausted cycle, then success, then rejections again.
    attempts = config.rate_limit.max_retries + 1
    route.side_effect = (
        [httpx.Response(429)] * attempts
        + [ok({"list": []})]
        + [httpx.Response(429)] * attempts * (limit - 1)
    )

    async with make_client(config, sleeper) as client:
        with pytest.raises(BybitError):
            await client.get_tickers()
        assert await client.get_tickers() == []

        for _ in range(limit - 1):
            with pytest.raises(BybitError):
                await client.get_tickers()

        # The success reset the counter, so there is one more cycle before the threshold.
        assert not client.circuit_open


@respx.mock
async def test_network_failures_do_not_trip_the_breaker(config, sleeper):
    """A dropped connection is not a block. Stopping on it is not allowed."""
    respx.get(f"{BASE}/v5/market/tickers").mock(
        side_effect=httpx.TimeoutException("timeout")
    )

    async with make_client(config, sleeper) as client:
        for _ in range(10):
            with pytest.raises(BybitError) as exc:
                await client.get_tickers()
            assert not isinstance(exc.value, BybitCircuitOpen)
        assert not client.circuit_open


@respx.mock
async def test_network_error_is_wrapped_not_raw_httpx(config, sleeper):
    """Regression: a raw httpx.TimeoutException escaped outward.

    Collectors catch BybitError and use it to decide whether to record a symbol
    rejection or crash. The raw httpx exception bypassed that branch
    and brought down the whole backfill instead of failing one stream.
    """
    respx.get(f"{BASE}/v5/market/tickers").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with make_client(config, sleeper) as client:
        with pytest.raises(BybitNetworkError) as exc:
            await client.get_tickers()

    assert isinstance(exc.value, BybitError)
    assert "ConnectError" in str(exc.value)


@respx.mock
async def test_collector_records_failure_on_network_error(config, sleeper, monkeypatch):
    """A network failure must remain a failure of ONE stream."""
    from src.collectors import base
    from src.collectors.ohlcv import OhlcvCollector

    recorded: list[tuple] = []

    async def fake_failure(symbol, stream, error):
        recorded.append((symbol, stream, error))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(base, "record_failure", fake_failure)
    monkeypatch.setattr(base, "record_success", noop)
    monkeypatch.setattr(base, "get_checkpoint", noop)

    respx.get(f"{BASE}/v5/market/kline").mock(
        side_effect=httpx.TimeoutException("timeout")
    )

    async with make_client(config, sleeper) as client:
        collector = OhlcvCollector(client, "1h")
        result = await collector.run(
            "SOLUSDT",
            start=datetime(2026, 8, 1, tzinfo=UTC),
            end=datetime(2026, 8, 2, tzinfo=UTC),
        )

    assert not result.ok
    assert len(recorded) == 1, "rejection not recorded — the exception bypassed the handler"


@respx.mock
async def test_ret_code_rate_limit_also_trips(config, sleeper):
    """The limit also arrives with HTTP 200 — the class is determined by retCode."""
    respx.get(f"{BASE}/v5/market/tickers").mock(
        return_value=httpx.Response(
            200, json={"retCode": 10018, "retMsg": "IP limit", "result": {}}
        )
    )
    limit = config.rate_limit.circuit_breaker.max_consecutive_rate_limit_failures

    async with make_client(config, sleeper) as client:
        for _ in range(limit - 1):
            with pytest.raises(BybitError):
                await client.get_tickers()
        with pytest.raises(BybitCircuitOpen):
            await client.get_tickers()


@respx.mock
async def test_message_explains_what_happened(config, sleeper):
    """A human reads the alert at three in the morning — they must understand the cause."""
    respx.get(f"{BASE}/v5/market/tickers").mock(return_value=httpx.Response(403))
    limit = config.rate_limit.circuit_breaker.max_consecutive_rate_limit_failures

    async with make_client(config, sleeper) as client:
        for _ in range(limit - 1):
            with pytest.raises(BybitError):
                await client.get_tickers()
        with pytest.raises(BybitCircuitOpen) as exc:
            await client.get_tickers()

    text = str(exc.value)
    assert "IP ban" in text
    assert "stopped" in text


def test_breaker_is_configured():
    cb = load_settings().bybit.rate_limit.circuit_breaker
    assert cb.max_consecutive_rate_limit_failures >= 1
