"""Transport tests: retries, pagination, retCode parsing.

The network is mocked via respx. Backoff delays are substituted so the test
does not actually sleep.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from src.core.config import load_settings
from src.exchange.bybit_client import (
    BybitAPIError,
    BybitClient,
    BybitError,
    BybitRateLimitError,
)
from src.exchange.rate_limiter import TokenBucket

BASE = "https://api.bybit.com"


class NoSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def config():
    return load_settings().bybit


@pytest.fixture
def sleeper():
    return NoSleep()


class FixedBackoff:
    """A constant delay — so the test can tell classes apart by value."""

    def __init__(self, value: float) -> None:
        self.value = value

    def delay(self, attempt: int) -> float:  # noqa: ARG002
        return self.value


NETWORK_DELAY = 1.0
RATE_LIMIT_DELAY = 50.0


def make_client(config, sleeper, *, now_ms: float = 0.0) -> BybitClient:
    # A large bucket: the tests check the transport, not throttling.
    return BybitClient(
        config,
        bucket=TokenBucket(rate=10_000, burst=10_000),
        network_backoff=FixedBackoff(NETWORK_DELAY),
        rate_limit_backoff=FixedBackoff(RATE_LIMIT_DELAY),
        sleep=sleeper,
        clock=lambda: now_ms,
    )


def ok(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": result})


def err(code: int, msg: str = "boom") -> httpx.Response:
    return httpx.Response(200, json={"retCode": code, "retMsg": msg, "result": {}})


@respx.mock
async def test_successful_request_returns_result(config, sleeper):
    respx.get(f"{BASE}/v5/market/tickers").mock(return_value=ok({"list": [{"symbol": "BTCUSDT"}]}))

    async with make_client(config, sleeper) as client:
        rows = await client.get_tickers()

    assert rows == [{"symbol": "BTCUSDT"}]
    assert sleeper.calls == []


@respx.mock
async def test_retries_on_server_error_then_succeeds(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(502),
        httpx.Response(503),
        ok({"list": [{"symbol": "ETHUSDT"}]}),
    ]

    async with make_client(config, sleeper) as client:
        rows = await client.get_tickers()

    assert rows == [{"symbol": "ETHUSDT"}]
    assert len(sleeper.calls) == 2


@respx.mock
async def test_retries_on_timeout(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [httpx.TimeoutException("timeout"), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        assert await client.get_tickers() == []

    assert len(sleeper.calls) == 1


@respx.mock
async def test_rate_limit_ret_code_is_retried(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [err(10006, "Too many visits"), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        assert await client.get_tickers() == []

    assert len(sleeper.calls) == 1


@respx.mock
async def test_parameter_error_raises_immediately(config, sleeper):
    """Retrying an error in our own parameters only burns the limit."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.mock(return_value=err(10001, "params error"))

    async with make_client(config, sleeper) as client:
        with pytest.raises(BybitAPIError) as exc:
            await client.get_tickers()

    assert exc.value.ret_code == 10001
    assert sleeper.calls == []
    assert route.call_count == 1


@respx.mock
async def test_gives_up_after_max_retries(config, sleeper):
    respx.get(f"{BASE}/v5/market/tickers").mock(return_value=httpx.Response(503))

    async with make_client(config, sleeper) as client:
        with pytest.raises(BybitError):
            await client.get_tickers()

    assert len(sleeper.calls) == config.rate_limit.max_retries


@respx.mock
async def test_rate_limit_error_type_is_preserved_after_exhaustion(config, sleeper):
    respx.get(f"{BASE}/v5/market/tickers").mock(return_value=err(10018, "IP limit"))

    async with make_client(config, sleeper) as client:
        with pytest.raises(BybitRateLimitError):
            await client.get_tickers()


@respx.mock
async def test_pagination_follows_cursor(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/instruments-info")
    route.side_effect = [
        ok({"list": [{"symbol": "A"}], "nextPageCursor": "c1"}),
        ok({"list": [{"symbol": "B"}], "nextPageCursor": "c2"}),
        ok({"list": [], "nextPageCursor": "c3"}),
    ]

    async with make_client(config, sleeper) as client:
        rows = await client._paginate("/v5/market/instruments-info", {"category": "linear"})

    assert [r["symbol"] for r in rows] == ["A", "B"]


@respx.mock
async def test_pagination_stops_on_empty_cursor(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/instruments-info")
    route.side_effect = [
        ok({"list": [{"symbol": "A"}], "nextPageCursor": "c1"}),
        ok({"list": [{"symbol": "B"}], "nextPageCursor": ""}),
    ]

    async with make_client(config, sleeper) as client:
        rows = await client._paginate("/v5/market/instruments-info", {})

    assert len(rows) == 2


@respx.mock
async def test_pagination_breaks_on_repeated_cursor(config, sleeper):
    """The exchange occasionally returns the same cursor — that would be an infinite loop."""
    route = respx.get(f"{BASE}/v5/market/instruments-info")
    route.side_effect = [
        ok({"list": [{"symbol": "A"}], "nextPageCursor": "same"}),
        ok({"list": [{"symbol": "B"}], "nextPageCursor": "same"}),
        ok({"list": [{"symbol": "C"}], "nextPageCursor": "same"}),
    ]

    async with make_client(config, sleeper) as client:
        rows = await client._paginate("/v5/market/instruments-info", {})

    assert [r["symbol"] for r in rows] == ["A", "B"]


@respx.mock
async def test_none_params_are_dropped(config, sleeper):
    """Bybit rejects empty values, so None must not be sent."""
    route = respx.get(f"{BASE}/v5/market/kline").mock(return_value=ok({"list": []}))

    async with make_client(config, sleeper) as client:
        await client.get_kline("BTCUSDT", "15", start=None, end=None)

    url = str(route.calls[0].request.url)
    assert "start=" not in url
    assert "end=" not in url
    assert "symbol=BTCUSDT" in url


@respx.mock
async def test_malformed_json_is_retried(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(200, text="<html>maintenance</html>"),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper) as client:
        assert await client.get_tickers() == []

    assert len(sleeper.calls) == 1


# ---------------------------------------------------------------------------
# Error classes: a network failure and a rate limit cost differently
# ---------------------------------------------------------------------------
@respx.mock
async def test_network_error_uses_fast_backoff(config, sleeper):
    """The server did not ask us to slow down — retry quickly."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [httpx.TimeoutException("timeout"), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [NETWORK_DELAY]


@respx.mock
async def test_server_error_is_treated_as_network(config, sleeper):
    """5xx is a problem on the exchange side, not us overdoing requests."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [httpx.Response(503), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [NETWORK_DELAY]


@respx.mock
async def test_http_429_uses_slow_backoff(config, sleeper):
    """The exchange said "too often" outright — a quick retry brings a ban closer."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [httpx.Response(429), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [RATE_LIMIT_DELAY]


@respx.mock
async def test_http_403_is_treated_as_rate_limit(config, sleeper):
    """Public endpoints have no authorization, so a 403 from Bybit
    is an already applied IP restriction, not "no permission"."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [httpx.Response(403), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [RATE_LIMIT_DELAY]


@respx.mock
async def test_rate_limit_ret_code_uses_slow_backoff(config, sleeper):
    """The limit also arrives with HTTP 200 — the class is determined by retCode."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [err(10006, "Too many visits"), ok({"list": []})]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [RATE_LIMIT_DELAY]


@respx.mock
async def test_error_class_is_reassessed_each_attempt(config, sleeper):
    """First 429, then a dropped connection: the second pause is already a network one."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(429),
        httpx.TimeoutException("timeout"),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [RATE_LIMIT_DELAY, NETWORK_DELAY]


# ---------------------------------------------------------------------------
# The server's hint on when to come back
# ---------------------------------------------------------------------------
@respx.mock
async def test_retry_after_overrides_shorter_backoff(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "120"}),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [120.0]


@respx.mock
async def test_retry_after_never_shortens_our_backoff(config, sleeper):
    """The exchange reports when the window opens, not when it is safe for us.

    After a series of rejections our exponential backoff may be longer — and it
    must win.
    """
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "1"}),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [RATE_LIMIT_DELAY]


@respx.mock
async def test_bybit_reset_timestamp_header_is_used(config, sleeper):
    """X-Bapi-Limit-Reset-Timestamp is an absolute timestamp in milliseconds."""
    now_ms = 1_786_000_000_000
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(
            429, headers={"X-Bapi-Limit-Reset-Timestamp": str(now_ms + 90_000)}
        ),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper, now_ms=now_ms) as client:
        await client.get_tickers()

    assert sleeper.calls == [pytest.approx(90.0)]


@respx.mock
async def test_absurd_retry_after_is_capped(config, sleeper):
    """A malformed header would otherwise hang the collector for hours."""
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "999999"}),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [config.rate_limit.max_retry_after_seconds]


@respx.mock
async def test_malformed_retry_after_falls_back_to_backoff(config, sleeper):
    route = respx.get(f"{BASE}/v5/market/tickers")
    route.side_effect = [
        # The value must be ASCII: otherwise httpx will not build the header.
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        ok({"list": []}),
    ]

    async with make_client(config, sleeper) as client:
        await client.get_tickers()

    assert sleeper.calls == [RATE_LIMIT_DELAY]


def test_retry_after_parsing_ignores_past_reset_timestamp():
    """A stale timestamp must not produce a negative pause."""
    from src.exchange.bybit_client import parse_retry_after

    now = 1_786_000_000_000
    result = parse_retry_after(
        {"x-bapi-limit-reset-timestamp": str(now - 5000)}, now_ms=now, cap=300
    )
    assert result is None


def test_retry_after_parsing_without_headers():
    from src.exchange.bybit_client import parse_retry_after

    assert parse_retry_after({}, now_ms=0, cap=300) is None


@respx.mock
async def test_delisting_announcements_stop_on_short_page(config, sleeper):
    route = respx.get(f"{BASE}/v5/announcements/index")
    route.side_effect = [
        ok({"list": [{"title": f"Delisting of A{i}USDT"} for i in range(100)]}),
        ok({"list": [{"title": "Delisting of BUSDT"}]}),
    ]

    async with make_client(config, sleeper) as client:
        rows = await client.get_delisting_announcements()

    assert len(rows) == 101
    assert route.call_count == 2
