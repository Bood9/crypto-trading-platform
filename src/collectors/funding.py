"""Funding collector.

The rate is converted to its 8-hour equivalent right at write time.

Reason: `fundingInterval` differs between symbols — 480 minutes for BTC,
240 or 60 for some alts. Percentiles of raw rates are not comparable
across pairs, and Setup A is built precisely on percentiles. Doing the
normalisation in the strategy would mean allowing the backtest and live
trading to diverge in how they convert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
from src.core.db import FundingHistory
from src.core.logging import get_logger
from src.core.models import DataStream

log = get_logger(__name__)

PAGE_LIMIT = 200

#: Funding intervals that Bybit uses, in minutes.
VALID_INTERVALS: frozenset[int] = frozenset({60, 120, 240, 480})

#: Page slicing step — the SHORTEST possible interval, not the symbol's
#: current interval (DEC-036). A symbol's interval changes over time:
#: if funding used to be hourly and is every 4 hours today, a window sized
#: by today's interval holds ~800 fundings, the exchange returns the 200
#: newest, and the older 600 are silently lost. With an hourly step a window
#: cannot hold more than 200 records at any interval.
PAGING_INTERVAL = timedelta(minutes=min(VALID_INTERVALS))


def infer_intervals(stamps: list[datetime], declared: int) -> list[int]:
    """Actual interval of each funding, inferred from its neighbours.

    The rate at moment `t` accrues over the period since the previous
    funding, so the primary source is the gap to the PREVIOUS record. If it
    is not a Bybit interval (first record or a hole in the data) — the
    gap to the next one. If that does not fit either — the symbol's
    declared interval, as before.
    """
    minutes = [
        int((b - a).total_seconds() // 60) for a, b in zip(stamps, stamps[1:], strict=False)
    ]
    out: list[int] = []
    for i in range(len(stamps)):
        before = minutes[i - 1] if i > 0 else None
        after = minutes[i] if i < len(minutes) else None
        if before in VALID_INTERVALS:
            out.append(before)
        elif after in VALID_INTERVALS:
            out.append(after)
        else:
            out.append(declared)
    return out


def normalize_to_8h(rate: Decimal, interval_minutes: int, target_hours: float = 8.0) -> Decimal:
    """Converts the rate to a common base.

    Linear scaling, not compounding: the rate is small (fractions of a
    percent), and percentiles are computed on the relative order, which
    linear conversion preserves exactly.
    """
    if interval_minutes <= 0:
        raise ValueError(f"interval_minutes must be positive, got {interval_minutes}")
    return rate * Decimal(str(target_hours * 60)) / Decimal(interval_minutes)


class FundingCollector(BaseCollector):
    stream = DataStream.FUNDING

    def __init__(self, client, interval_minutes: int = 480) -> None:  # noqa: ANN001
        super().__init__(client)
        # The symbol's real interval from instruments-info. Affects both
        # normalisation and the watchdog threshold.
        self.interval_minutes = interval_minutes
        self.interval = timedelta(minutes=interval_minutes)

    async def fetch(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[dict], int]:
        rows: list[dict] = []
        requests = 0
        cursor = start
        seen: set[datetime] = set()

        while cursor < end:
            window_end = page_window_end(cursor, PAGING_INTERVAL, PAGE_LIMIT, end)
            page = await self._client.get_funding_history(
                symbol, start=ms(cursor), end=ms(window_end), limit=PAGE_LIMIT
            )
            requests += 1

            if not page:
                cursor = window_end
                continue

            newest = cursor
            accepted = 0
            for item in page:
                ts = from_ms(item["fundingRateTimestamp"])
                if ts in seen or ts >= end:
                    continue
                seen.add(ts)
                accepted += 1
                newest = max(newest, ts)
                rate = dec(item["fundingRate"])
                rows.append({"symbol": symbol, "ts": ts, "rate": rate})

            cursor = next_cursor(
                cursor,
                window_end,
                accepted=accepted,
                newest=newest,
                interval=PAGING_INTERVAL,
            )

        # Interval and normalised rate follow the actual step, not the
        # symbol's current interval (DEC-025, DEC-036).
        rows.sort(key=lambda r: r["ts"])
        intervals = infer_intervals([r["ts"] for r in rows], self.interval_minutes)
        for row, minutes in zip(rows, intervals, strict=True):
            row["interval_minutes"] = minutes
            row["rate_8h"] = normalize_to_8h(row["rate"], minutes)

        return rows, requests

    async def store(self, rows: list[dict]) -> int:
        return await upsert_chunked(
            FundingHistory,
            rows,
            index_elements=[FundingHistory.symbol, FundingHistory.ts],
            update_columns=("rate", "interval_minutes", "rate_8h"),
        )
        return len(rows)


async def collect_funding(
    client,  # noqa: ANN001
    symbol: str,
    start: datetime,
    end: datetime | None = None,
    interval_minutes: int = 480,
) -> CollectResult:
    return await FundingCollector(client, interval_minutes).run(
        symbol, start=start, end=end or datetime.now(UTC)
    )
