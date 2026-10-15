"""Candle collector.

Candles are not just price. Their `turnover` field ranks the point-in-time
universe (DEC-003), so historical turnover comes to us for free
and needs no separate source.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.collectors.base import (
    BaseCollector,
    CollectResult,
    dec,
    from_ms,
    ms,
    next_cursor,
    page_window_end,
)
from src.core.bulk import upsert_chunked
from src.core.db import Ohlcv
from src.core.logging import get_logger
from src.core.models import DataStream

log = get_logger(__name__)

#: Our notation → values of the `interval` parameter in the Bybit API.
TIMEFRAMES: dict[str, tuple[str, timedelta, DataStream]] = {
    "15m": ("15", timedelta(minutes=15), DataStream.KLINE_15M),
    "1h": ("60", timedelta(hours=1), DataStream.KLINE_1H),
    "4h": ("240", timedelta(hours=4), DataStream.KLINE_4H),
}

#: Maximum bars per Bybit response.
PAGE_LIMIT = 1000


class OhlcvCollector(BaseCollector):
    def __init__(self, client, timeframe: str) -> None:  # noqa: ANN001
        if timeframe not in TIMEFRAMES:
            # P-001: an unknown timeframe is not replaced by the nearest one.
            raise ValueError(
                f"unknown timeframe {timeframe!r}, available: {sorted(TIMEFRAMES)}"
            )
        super().__init__(client)
        self.timeframe = timeframe
        self.api_interval, self.interval, self.stream = TIMEFRAMES[timeframe]

    async def fetch(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[dict], int]:
        """Walks forward in windows from `start`.

        Bybit returns bars newest-first and offers no cursor on this
        endpoint, so we move the window ourselves: the next request starts
        right after the newest bar received.
        """
        rows: list[dict] = []
        requests = 0
        cursor = start
        seen: set[datetime] = set()

        while cursor < end:
            window_end = page_window_end(cursor, self.interval, PAGE_LIMIT, end)
            page = await self._client.get_kline(
                symbol,
                self.api_interval,
                start=ms(cursor),
                end=ms(window_end),
                limit=PAGE_LIMIT,
            )
            requests += 1

            if not page:
                # A hole in history: the symbol may have no data in this
                # window (delisted and relisted, trading halt).
                # Jump over the window rather than stop — otherwise we lose
                # everything beyond it.
                cursor = window_end
                continue

            newest = cursor
            accepted = 0
            for bar in page:
                ts = from_ms(bar[0])
                if ts in seen or ts >= end:
                    continue
                seen.add(ts)
                accepted += 1
                newest = max(newest, ts)
                rows.append(
                    {
                        "symbol": symbol,
                        "timeframe": self.timeframe,
                        "ts": ts,
                        "open": dec(bar[1]),
                        "high": dec(bar[2]),
                        "low": dec(bar[3]),
                        "close": dec(bar[4]),
                        "volume": dec(bar[5]),
                        "turnover": dec(bar[6]),
                    }
                )

            # Progress guarantee: without it, a response consisting entirely
            # of bars already seen would loop the sweep forever.
            cursor = next_cursor(
                cursor,
                window_end,
                accepted=accepted,
                newest=newest,
                interval=self.interval,
            )

        return rows, requests

    async def store(self, rows: list[dict]) -> int:
        # Overwrite: the exchange occasionally revises a closed bar, and the
        # record must converge to its version, not to the first one received.
        return await upsert_chunked(
            Ohlcv,
            rows,
            index_elements=[Ohlcv.symbol, Ohlcv.timeframe, Ohlcv.ts],
            update_columns=("open", "high", "low", "close", "volume", "turnover"),
        )


async def collect_ohlcv(
    client, symbol: str, timeframe: str, start: datetime, end: datetime | None = None
) -> CollectResult:  # noqa: ANN001
    return await OhlcvCollector(client, timeframe).run(
        symbol, start=start, end=end or datetime.now(UTC)
    )
