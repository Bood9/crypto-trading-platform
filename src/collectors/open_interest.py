"""Open Interest collector.

OI is the basis of the Setup A trigger (DEC-001). The trigger is defined as
a sharp one-hour OI contraction in the direction of the price move;
liquidations play no part in it, neither in the backtest nor live.

Reconnaissance on 2026-08-07 showed that OI depth is not limited to six
months: data is returned from the symbol's listing, and for some pairs
it starts even earlier than the 15m candles.
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
from src.core.db import OpenInterest
from src.core.logging import get_logger
from src.core.models import DataStream

log = get_logger(__name__)

#: Our notation → `intervalTime` values in the API.
OI_INTERVALS: dict[str, timedelta] = {
    "5min": timedelta(minutes=5),
    "15min": timedelta(minutes=15),
    "30min": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

PAGE_LIMIT = 200


class OpenInterestCollector(BaseCollector):
    stream = DataStream.OI

    def __init__(self, client, oi_interval: str = "1h") -> None:  # noqa: ANN001
        if oi_interval not in OI_INTERVALS:
            # P-001: an unknown interval is not replaced by the nearest one.
            raise ValueError(
                f"unknown OI interval {oi_interval!r}, "
                f"available: {sorted(OI_INTERVALS)}"
            )
        super().__init__(client)
        self.oi_interval = oi_interval
        self.interval = OI_INTERVALS[oi_interval]

    async def fetch(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[dict], int]:
        """Sweep in windows; within a window, cursor pagination is handled by the client."""
        rows: list[dict] = []
        requests = 0
        cursor = start
        seen: set[datetime] = set()

        while cursor < end:
            window_end = page_window_end(cursor, self.interval, PAGE_LIMIT, end)
            page = await self._client.get_open_interest(
                symbol,
                self.oi_interval,
                start=ms(cursor),
                end=ms(window_end),
                limit=PAGE_LIMIT,
            )
            requests += 1

            if not page:
                cursor = window_end
                continue

            newest = cursor
            accepted = 0
            for item in page:
                ts = from_ms(item["timestamp"])
                if ts in seen or ts >= end:
                    continue
                seen.add(ts)
                accepted += 1
                newest = max(newest, ts)
                rows.append(
                    {
                        "symbol": symbol,
                        "interval": self.oi_interval,
                        "ts": ts,
                        "open_interest": dec(item["openInterest"]),
                    }
                )

            cursor = next_cursor(
                cursor,
                window_end,
                accepted=accepted,
                newest=newest,
                interval=self.interval,
            )

        return rows, requests

    async def store(self, rows: list[dict]) -> int:
        return await upsert_chunked(
            OpenInterest,
            rows,
            index_elements=[
                OpenInterest.symbol,
                OpenInterest.interval,
                OpenInterest.ts,
            ],
            update_columns=("open_interest",),
        )


async def collect_open_interest(
    client,  # noqa: ANN001
    symbol: str,
    start: datetime,
    end: datetime | None = None,
    oi_interval: str = "1h",
) -> CollectResult:
    return await OpenInterestCollector(client, oi_interval).run(
        symbol, start=start, end=end or datetime.now(UTC)
    )
