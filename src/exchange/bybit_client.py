"""Async Bybit v5 client.

DEC-004: the only way to access the exchange. ccxt is not used — neither
here nor in execution. All endpoints the system needs are v5-specific, and
an abstraction layer would get in the way of debugging cursor pagination
and rate-limit accounting, i.e. exactly what the main difficulty of data
collection consists of.

In Phase 1 request signing is not needed: the endpoints used are public.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any

import httpx

from src.core.config import BybitConfig
from src.core.logging import get_logger
from src.exchange.rate_limiter import Backoff, TokenBucket

log = get_logger(__name__)


class BybitError(Exception):
    """Base error of an exchange call."""


class BybitNetworkError(BybitError):
    """No response received: timeout, disconnect, transport failure.

    A separate type is needed so that a raw httpx exception does not leak out.
    Collectors catch BybitError and decide from it whether to record a symbol
    failure or fail entirely; a raw TimeoutException bypassed that branch and
    brought down the whole backfill instead of failing one stream.
    """


class BybitAPIError(BybitError):
    """The exchange answered with a non-zero retCode."""

    def __init__(self, ret_code: int, ret_msg: str, endpoint: str) -> None:
        super().__init__(f"{endpoint}: retCode={ret_code} {ret_msg}")
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint


class BybitRateLimitError(BybitAPIError):
    """Request limit exceeded. Retry, but with a longer delay."""


class BybitCircuitOpen(BybitError):
    """The circuit breaker tripped: the collector is stopped.

    Raised when the exchange keeps answering 403/429 after several full
    cycles of five-minute pauses. By then it is no longer about a limit
    window but an IP ban or a regional block, and new attempts only extend
    the block.

    Deliberately not derived from BybitAPIError: this is not an exchange
    response but our decision to stop working. It must not be retried.
    """


# retCodes for which a retry makes sense. The rest are errors on our side,
# a retry gives the same result and only burns the limit.
_RETRYABLE_RET_CODES = frozenset({10002, 10006, 10016, 10018, 130150})
_RATE_LIMIT_RET_CODES = frozenset({10006, 10018})

_RETRYABLE_STATUS = frozenset({403, 429, 500, 502, 503, 504})
# A 403 from Bybit is an already applied IP restriction, not "no permission":
# public endpoints have no authorization. Treat it like a rate-limit breach,
# otherwise fast retries will extend the block.
_RATE_LIMIT_STATUS = frozenset({403, 429})

# Headers by which the exchange says when it is OK to come back.
_RETRY_AFTER_HEADER = "retry-after"
_BYBIT_RESET_HEADER = "x-bapi-limit-reset-timestamp"


def parse_retry_after(headers: Mapping[str, str], *, now_ms: float, cap: float) -> float | None:
    """How long to wait according to the exchange, in seconds.

    The server's hint is more accurate than any exponent of ours, but it
    cannot be trusted without a cap: an incorrect or stale header would
    otherwise hang the collector for hours.
    """
    raw = headers.get(_RETRY_AFTER_HEADER)
    if raw:
        try:
            # Seconds. HTTP also allows a date, but Bybit does not use it.
            return max(0.0, min(float(raw), cap))
        except ValueError:
            pass

    reset = headers.get(_BYBIT_RESET_HEADER)
    if reset:
        try:
            delta = (float(reset) - now_ms) / 1000.0
        except ValueError:
            return None
        if delta > 0:
            return min(delta, cap)
    return None


class BybitClient:
    """Wrapper over the public REST API v5.

    Holds one httpx.AsyncClient and a shared token bucket: Bybit's limit is
    counted per IP, so it must not be split between collectors.
    """

    def __init__(
        self,
        config: BybitConfig,
        *,
        testnet: bool = False,
        client: httpx.AsyncClient | None = None,
        bucket: TokenBucket | None = None,
        network_backoff: Backoff | None = None,
        rate_limit_backoff: Backoff | None = None,
        sleep: Any = None,
        clock: Any = None,
    ) -> None:
        self._config = config
        self._base_url = config.base_url(testnet=testnet)
        rl = config.rate_limit

        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=rl.timeout_seconds,
            headers={"User-Agent": "crypto-bot/0.1"},
        )
        self._owns_client = client is None
        self._bucket = bucket or TokenBucket(rl.requests_per_second, rl.burst)

        # Two policies, not one: a connection drop and "too often" cost
        # differently. The server did not ask to slow down in the first case —
        # and explicitly asked in the second, where the next such response
        # brings an IP ban closer.
        self._network_backoff = network_backoff or Backoff(
            rl.backoff.network.base_seconds, rl.backoff.network.max_seconds
        )
        self._rate_limit_backoff = rate_limit_backoff or Backoff(
            rl.backoff.rate_limit.base_seconds, rl.backoff.rate_limit.max_seconds
        )
        self._respect_retry_after = rl.respect_retry_after
        self._max_retry_after = rl.max_retry_after_seconds
        self._max_retries = rl.max_retries
        self._sleep = sleep or asyncio.sleep
        self._now_ms = clock or (lambda: time.time() * 1000)

        # Circuit breaker. Count consecutive EXHAUSTED cycles that ended
        # on a rate-limit breach: any successful response resets the
        # counter.
        self._max_rate_limit_cycles = rl.circuit_breaker.max_consecutive_rate_limit_failures
        self._rate_limit_cycles = 0
        self._circuit_open = False

    @property
    def circuit_open(self) -> bool:
        return self._circuit_open

    def _trip_circuit(self, endpoint: str, last_error: Exception) -> BybitCircuitOpen:
        self._circuit_open = True
        message = (
            f"circuit breaker tripped after {self._rate_limit_cycles} consecutive "
            f"exhausted cycles on rate-limit breaches ({endpoint}). "
            "Looks like an IP ban or a regional block, not a limit window: "
            "further attempts extend the block. Collector stopped."
        )
        log.error(
            "bybit.circuit_open",
            endpoint=endpoint,
            cycles=self._rate_limit_cycles,
            last_error=str(last_error),
        )
        return BybitCircuitOpen(message)

    async def __aenter__(self) -> BybitClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -----------------------------------------------------------------
    # Transport
    # -----------------------------------------------------------------
    async def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One request with retries. Returns the content of `result`."""
        if self._circuit_open:
            raise BybitCircuitOpen(
                "circuit breaker open: collector stopped, "
                "intervention and restart required"
            )

        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_error: Exception | None = None
        # The class of the previous error determines the pause policy. The
        # initial value is unused: there are no pauses on the first attempt.
        throttled = False
        server_hint: float | None = None

        for attempt in range(self._max_retries + 1):
            if attempt:
                delay = self._retry_delay(attempt - 1, throttled=throttled, hint=server_hint)
                log.warning(
                    "bybit.retry",
                    endpoint=endpoint,
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    kind="rate_limit" if throttled else "network",
                    from_server=server_hint is not None,
                    reason=str(last_error),
                )
                await self._sleep(delay)
                server_hint = None

            await self._bucket.acquire()

            try:
                response = await self._client.get(endpoint, params=clean)
            except httpx.RequestError as exc:
                # httpx.RequestError, not TransportError: DecodingError
                # and TooManyRedirects are RequestError but NOT
                # TransportError, and used to bypass the handler.
                #
                # Wrap immediately: only BybitError may leave the client,
                # otherwise the calling code cannot tell a network failure
                # from a program crash.
                last_error = BybitNetworkError(f"{endpoint}: {type(exc).__name__}: {exc}")
                throttled = False
                continue
            except Exception as exc:
                # Everything else is not the network but a surprise: InvalidURL
                # and StreamError are not even in HTTPError. Retrying is
                # pointless (a retry gives the same), but letting a foreign
                # type out is not allowed either.
                #
                # asyncio.CancelledError does not get here: since Python 3.8
                # it derives from BaseException, and swallowing it would be
                # a mistake — it is flow control, not a failure.
                raise BybitError(
                    f"{endpoint}: unexpected transport error "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            if response.status_code in _RETRYABLE_STATUS:
                throttled = response.status_code in _RATE_LIMIT_STATUS
                if throttled:
                    server_hint = self._server_hint(response.headers)
                last_error = BybitError(f"{endpoint}: HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise BybitError(f"{endpoint}: HTTP {response.status_code} {response.text[:200]}")

            try:
                payload = response.json()
            except Exception as exc:
                # Not only ValueError: the decoder may raise
                # UnicodeDecodeError or trip over a broken encoding.
                last_error = BybitError(f"{endpoint}: response is not JSON")
                throttled = False
                log.warning("bybit.bad_json", endpoint=endpoint, error=str(exc))
                continue

            if not isinstance(payload, dict):
                # Bybit returns an object. An array or a string means we are
                # talking to the wrong endpoint — calling .get() on them
                # would raise AttributeError past all handlers.
                last_error = BybitError(
                    f"{endpoint}: expected a JSON object, got {type(payload).__name__}"
                )
                throttled = False
                continue

            ret_code = payload.get("retCode")
            if ret_code == 0:
                # Success resets the counter: the circuit breaker counts
                # consecutive failures, not their total over a run.
                self._rate_limit_cycles = 0
                result = payload.get("result") or {}
                if not isinstance(result, dict):
                    raise BybitError(
                        f"{endpoint}: the result field is not an object "
                        f"({type(result).__name__})"
                    )
                return result

            ret_msg = str(payload.get("retMsg", ""))
            # retCode comes as a number, but that cannot be relied on:
            # int() on garbage would raise ValueError past all handlers.
            try:
                code = int(ret_code) if ret_code is not None else -1
            except (TypeError, ValueError):
                raise BybitError(
                    f"{endpoint}: non-numeric retCode {ret_code!r} (retMsg={ret_msg!r})"
                ) from None
            if ret_code in _RATE_LIMIT_RET_CODES:
                # The limit can also arrive with HTTP 200 — look at retCode.
                throttled = True
                server_hint = self._server_hint(response.headers)
                last_error = BybitRateLimitError(code, ret_msg, endpoint)
                continue
            if ret_code in _RETRYABLE_RET_CODES:
                throttled = False
                last_error = BybitAPIError(code, ret_msg, endpoint)
                continue

            raise BybitAPIError(code, ret_msg, endpoint)

        assert last_error is not None

        # The cycle is exhausted. If it ended on a rate-limit breach —
        # it is a candidate for tripping the circuit breaker.
        if throttled:
            self._rate_limit_cycles += 1
            if self._rate_limit_cycles >= self._max_rate_limit_cycles:
                raise self._trip_circuit(endpoint, last_error) from last_error
            log.warning(
                "bybit.rate_limit_cycle_exhausted",
                endpoint=endpoint,
                cycles=self._rate_limit_cycles,
                limit=self._max_rate_limit_cycles,
            )

        raise last_error

    def _server_hint(self, headers: Mapping[str, str]) -> float | None:
        if not self._respect_retry_after:
            return None
        return parse_retry_after(
            headers, now_ms=self._now_ms(), cap=self._max_retry_after
        )

    def _retry_delay(self, attempt: int, *, throttled: bool, hint: float | None) -> float:
        """Pause before a retry.

        The server's hint beats the computation but cannot lower it: the
        exchange says when the window opens, not when it is safe for us to
        come back after a series of failures.
        """
        backoff = self._rate_limit_backoff if throttled else self._network_backoff
        computed = backoff.delay(attempt)
        return max(computed, hint) if hint is not None else computed

    async def _paginate(
        self,
        endpoint: str,
        params: dict[str, Any],
        *,
        max_pages: int = 1000,
    ) -> list[dict[str, Any]]:
        """Iterate by `nextPageCursor`.

        Bybit returns a non-empty cursor even on the last page, so the end is
        signalled by an empty list, not by the absence of a cursor.
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()

        for _ in range(max_pages):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            result = await self._request(endpoint, page_params)

            page = result.get("list") or []
            rows.extend(page)

            cursor = result.get("nextPageCursor") or None
            if not page or not cursor:
                break
            # Loop protection: the exchange occasionally repeats a cursor.
            if cursor in seen:
                log.warning("bybit.cursor_loop", endpoint=endpoint, cursor=cursor[:32])
                break
            seen.add(cursor)

        return rows

    # -----------------------------------------------------------------
    # Market data
    # -----------------------------------------------------------------
    async def get_instruments(self, category: str | None = None) -> list[dict[str, Any]]:
        """All instruments of a category. Live only — dead ones are not here."""
        return await self._paginate(
            "/v5/market/instruments-info",
            {"category": category or self._config.category, "limit": 1000},
        )

    async def get_tickers(self, category: str | None = None) -> list[dict[str, Any]]:
        result = await self._request(
            "/v5/market/tickers", {"category": category or self._config.category}
        )
        return result.get("list") or []

    async def get_risk_limit(
        self, symbol: str, category: str | None = None
    ) -> list[dict[str, Any]]:
        """Risk tiers of a symbol. The first tier gives the MMR for our sizes."""
        result = await self._request(
            "/v5/market/risk-limit",
            {"category": category or self._config.category, "symbol": symbol},
        )
        return result.get("list") or []

    async def get_kline(
        self,
        symbol: str,
        interval: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 1000,
        category: str | None = None,
    ) -> list[list[str]]:
        """Candles. Returned newest to oldest, at most 1000 per request.

        There is no cursor here — the window is set via start/end.
        """
        result = await self._request(
            "/v5/market/kline",
            {
                "category": category or self._config.category,
                "symbol": symbol,
                "interval": interval,
                "start": start,
                "end": end,
                "limit": limit,
            },
        )
        return result.get("list") or []

    async def get_funding_history(
        self,
        symbol: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 200,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._request(
            "/v5/market/funding/history",
            {
                "category": category or self._config.category,
                "symbol": symbol,
                "startTime": start,
                "endTime": end,
                "limit": limit,
            },
        )
        return result.get("list") or []

    async def get_open_interest(
        self,
        symbol: str,
        interval: str = "1h",
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 200,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._paginate(
            "/v5/market/open-interest",
            {
                "category": category or self._config.category,
                "symbol": symbol,
                "intervalTime": interval,
                "startTime": start,
                "endTime": end,
                "limit": limit,
            },
        )

    async def get_delisting_announcements(self, max_pages: int = 20) -> list[dict[str, Any]]:
        """Delisting announcements.

        An auxiliary source for cross-checking. The main enumeration of dead
        pairs is the public.bybit.com archive — it is more complete (1870
        directories versus 800 live instruments).
        """
        rows: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            result = await self._request(
                "/v5/announcements/index",
                {"locale": "en-US", "type": "delistings", "limit": 100, "page": page},
            )
            page_rows = result.get("list") or []
            rows.extend(page_rows)
            if len(page_rows) < 100:
                break
        return rows
