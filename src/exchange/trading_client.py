"""Signed requests to the Bybit trading API — testnet ONLY. Phase 5.

Two properties are held by construction, not by convention.

**Testnet only.** The client takes the address from `bybit.testnet_base_url`
and has no other — there is no parameter or flag that could point it at the
main exchange. If the testnet address in the config is mistakenly replaced
with the main exchange's address, the client refuses to be created. Trading
real money is a separate decision of the next phase, and it will require
separate code, not a switch.

**Order creation is sent exactly once.** Retrying a POST after a timeout is
the classic way to open two positions instead of one: the request may have
arrived and executed while the response was lost. The client does not retry
such errors but reports them upward as "response lost", and the retry is
done by the executor — with THE SAME idempotency key, which the exchange
will reject as a duplicate (DEC-027). Reads (positions, open orders) are
safe to retry, and they are retried.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx
from pydantic import SecretStr

from src.core.config import BybitConfig

#: The window within which the exchange accepts a signed request.
RECV_WINDOW_MS = 5000


class TradingError(Exception):
    """Base trading client error."""


class TradingNetworkError(TradingError):
    """The request may have arrived but the response did not. For POST this is "response lost"."""


class TradingAPIError(TradingError):
    def __init__(self, ret_code: int, ret_msg: str, endpoint: str) -> None:
        super().__init__(f"{endpoint}: retCode={ret_code} {ret_msg}")
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint


def sign(
    secret: str, timestamp_ms: int, api_key: str, recv_window: int, payload: str
) -> str:
    """Bybit v5 signature: HMAC-SHA256 of `timestamp + key + recvWindow + payload`.

    `payload` is the query string for GET or the JSON body for POST, exactly
    as it is sent to the exchange. Any mismatch between what is signed and
    what is sent is an exchange rejection, so both strings are built here,
    by one piece of code.
    """
    message = f"{timestamp_ms}{api_key}{recv_window}{payload}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def assert_testnet(url: str) -> str:
    """Rejects the address if it does not look like testnet. Checked by host."""
    host = urlparse(url).hostname or ""
    if "testnet" not in host:
        raise ValueError(
            f"the trading client works only with testnet, but {url!r} was given. "
            "The main exchange is a separate decision of the next phase, not a setting"
        )
    return url


class BybitTradingClient:
    def __init__(
        self,
        config: BybitConfig,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
        read_attempts: int = 3,
    ) -> None:
        key = api_key.get_secret_value()
        secret = api_secret.get_secret_value()
        if not key or not secret:
            # P-001: without a key it is not "let's try unsigned" but a rejection.
            raise ValueError("no testnet key: BYBIT_API_KEY and BYBIT_API_SECRET in .env")
        self._key = key
        self._secret = secret
        self._base = assert_testnet(config.testnet_base_url)
        self._category = config.category
        self._clock = clock or time.time
        self._read_attempts = max(1, read_attempts)
        self._client = client or httpx.AsyncClient(
            base_url=self._base,
            timeout=config.rate_limit.timeout_seconds,
            headers={"User-Agent": "crypto-bot/0.1"},
        )
        self._owns_client = client is None

    @property
    def category(self) -> str:
        return self._category

    async def __aenter__(self) -> BybitTradingClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, payload: str) -> dict[str, str]:
        timestamp = int(self._clock() * 1000)
        return {
            "X-BAPI-API-KEY": self._key,
            "X-BAPI-TIMESTAMP": str(timestamp),
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-SIGN": sign(self._secret, timestamp, self._key, RECV_WINDOW_MS, payload),
        }

    @staticmethod
    def _unwrap(endpoint: str, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise TradingNetworkError(
                f"{endpoint}: response is not JSON (HTTP {response.status_code})"
            ) from exc
        code = int(data.get("retCode", -1))
        if code != 0:
            raise TradingAPIError(code, str(data.get("retMsg", "")), endpoint)
        return data.get("result") or {}

    async def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        """Signed read. Retried: a read changes nothing."""
        query = urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))
        last: Exception | None = None
        for _ in range(self._read_attempts):
            try:
                response = await self._client.get(
                    f"{endpoint}?{query}" if query else endpoint,
                    headers=self._headers(query),
                )
            except httpx.RequestError as exc:
                last = exc
                continue
            if response.status_code >= 500:
                last = TradingNetworkError(f"{endpoint}: HTTP {response.status_code}")
                continue
            return self._unwrap(endpoint, response)
        raise TradingNetworkError(f"{endpoint}: read failed: {last}") from last

    async def post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        """Signed write. NOT retried — see the module header."""
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True)
        headers = self._headers(payload) | {"Content-Type": "application/json"}
        try:
            response = await self._client.post(endpoint, content=payload, headers=headers)
        except httpx.RequestError as exc:
            raise TradingNetworkError(f"{endpoint}: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 500:
            raise TradingNetworkError(f"{endpoint}: HTTP {response.status_code}")
        return self._unwrap(endpoint, response)


__all__ = [
    "RECV_WINDOW_MS",
    "BybitTradingClient",
    "TradingAPIError",
    "TradingError",
    "TradingNetworkError",
    "assert_testnet",
    "sign",
]
