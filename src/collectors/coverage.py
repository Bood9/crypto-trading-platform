"""Backfill size estimate and data coverage report.

Two jobs, both about "what are we going to download at all, and what did
we actually download".

The estimate is needed before launch: the bulk backfill is the longest
operation of the phase, and learning its cost halfway through is unpleasant.

The coverage report fills the (symbol, month, stream) matrix. Availability
cannot be counted per symbol: MULTIUSDT returns candles but not OI
and funding — this is a class of cases, not an exception.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from src.core.bulk import upsert_chunked
from src.core.db import (
    FundingHistory,
    Ohlcv,
    OpenInterest,
    StreamAvailability,
    session_scope,
)
from src.core.logging import get_logger
from src.core.models import DataStream

log = get_logger(__name__)

#: Step of each stream. Funding is given for the standard 8-hour
#: interval; for symbols with a different interval the expectation is
#: adjusted separately, see expected_bars.
STREAM_INTERVAL: dict[DataStream, timedelta] = {
    DataStream.KLINE_15M: timedelta(minutes=15),
    DataStream.KLINE_1H: timedelta(hours=1),
    DataStream.KLINE_4H: timedelta(hours=4),
    DataStream.OI: timedelta(hours=1),
    DataStream.FUNDING: timedelta(hours=8),
}

#: How many records one exchange response returns. Candles and the other
#: endpoints have different limits, and that triples the request volume.
PAGE_SIZE: dict[DataStream, int] = {
    DataStream.KLINE_15M: 1000,
    DataStream.KLINE_1H: 1000,
    DataStream.KLINE_4H: 1000,
    DataStream.OI: 200,
    DataStream.FUNDING: 200,
}

#: Average on-disk row size before compression, bytes. A rough estimate:
#: it is there to tell gigabytes from terabytes, not for precision.
BYTES_PER_ROW = 110


def month_key(moment: datetime | date) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def month_bounds(key: str) -> tuple[datetime, datetime]:
    """Month bounds in UTC: [start, start of the next)."""
    year, month = (int(p) for p in key.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, tzinfo=UTC) + timedelta(days=1)
    return start, end


def months_between(start: datetime, end: datetime) -> list[str]:
    keys: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return keys


def expected_bars(
    stream: DataStream,
    start: datetime,
    end: datetime,
    *,
    funding_interval_minutes: int = 480,
) -> int:
    """How many records the window should hold at full coverage."""
    if end <= start:
        return 0
    if stream is DataStream.FUNDING:
        step = timedelta(minutes=funding_interval_minutes)
    else:
        step = STREAM_INTERVAL[stream]
    return int((end - start).total_seconds() // step.total_seconds())


# ---------------------------------------------------------------------------
# Estimate before launch
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BackfillEstimate:
    symbols: int
    streams: int
    requests: int
    rows: int
    seconds: float
    bytes_raw: int

    @property
    def gigabytes(self) -> float:
        return self.bytes_raw / 1024**3

    def human_time(self) -> str:
        if self.seconds < 90:
            return f"{self.seconds:.0f}s"
        if self.seconds < 5400:
            return f"{self.seconds / 60:.0f} min"
        return f"{self.seconds / 3600:.1f} h"


def estimate_backfill(
    symbols: int,
    streams: list[DataStream],
    years: float,
    *,
    requests_per_second: float,
    funding_interval_minutes: int = 480,
) -> BackfillEstimate:
    """Rough cost of the bulk backfill.

    Counts the upper bound: assumes full coverage for every
    symbol. The real volume will be smaller — some pairs are younger than
    the window. Underestimating here is more dangerous than overestimating.
    """
    end = datetime.now(UTC)
    start = end - timedelta(days=years * 365.25)

    rows_per_symbol = 0
    requests_per_symbol = 0
    for stream in streams:
        bars = expected_bars(
            stream, start, end, funding_interval_minutes=funding_interval_minutes
        )
        rows_per_symbol += bars
        page = PAGE_SIZE[stream]
        requests_per_symbol += -(-bars // page)  # ceiling division

    rows = rows_per_symbol * symbols
    requests = requests_per_symbol * symbols
    return BackfillEstimate(
        symbols=symbols,
        streams=len(streams),
        requests=requests,
        rows=rows,
        seconds=requests / requests_per_second if requests_per_second else 0.0,
        bytes_raw=rows * BYTES_PER_ROW,
    )


# ---------------------------------------------------------------------------
# Actual coverage
# ---------------------------------------------------------------------------
_TABLE_FOR_STREAM = {
    DataStream.KLINE_15M: (Ohlcv, "15m"),
    DataStream.KLINE_1H: (Ohlcv, "1h"),
    DataStream.KLINE_4H: (Ohlcv, "4h"),
    DataStream.OI: (OpenInterest, None),
    DataStream.FUNDING: (FundingHistory, None),
}


async def count_by_month(symbol: str, stream: DataStream) -> dict[str, int]:
    """Actual record count per month.

    Grouping happens in Postgres: pulling tens of thousands of rows
    into Python just to count them is the only operation here that
    could hit a memory limit.
    """
    table, timeframe = _TABLE_FOR_STREAM[stream]
    bucket = func.to_char(table.ts, "YYYY-MM").label("month")

    query = select(bucket, func.count()).where(table.symbol == symbol)
    if timeframe is not None:
        query = query.where(table.timeframe == timeframe)
    if stream is DataStream.OI:
        query = query.where(table.interval == "1h")
    query = query.group_by(bucket)

    async with session_scope() as session:
        rows = (await session.execute(query)).all()
    return {month: count for month, count in rows}


@dataclass(frozen=True)
class Gap:
    symbol: str
    month: str
    stream: DataStream
    actual: int
    expected: int

    @property
    def completeness(self) -> float:
        return self.actual / self.expected if self.expected else 0.0

    @property
    def absent(self) -> bool:
        """The stream is absent entirely, not merely incomplete."""
        return self.actual == 0


async def build_availability(
    symbol: str,
    streams: list[DataStream],
    start: datetime,
    end: datetime,
    *,
    funding_interval_minutes: int = 480,
    completeness_threshold: float = 0.9,
) -> list[Gap]:
    """Fills the availability matrix and returns the gaps found.

    `completeness_threshold` is the share from which a month counts as
    covered. Requiring 100% is impossible: the exchange routinely skips
    individual bars, and strict equality would throw out almost the whole universe.
    """
    gaps: list[Gap] = []
    rows: list[dict] = []
    now = datetime.now(UTC)

    for stream in streams:
        actual = await count_by_month(symbol, stream)

        for key in months_between(start, end):
            m_start, m_end = month_bounds(key)
            # Clip to the requested window bounds and to the current
            # moment: an unfinished month must not look like a gap.
            window_start = max(m_start, start)
            window_end = min(m_end, end, now)
            expected = expected_bars(
                stream,
                window_start,
                window_end,
                funding_interval_minutes=funding_interval_minutes,
            )
            if expected <= 0:
                continue

            count = actual.get(key, 0)
            available = count >= expected * completeness_threshold

            rows.append(
                {
                    "symbol": symbol,
                    "month": key,
                    "stream": stream.value,
                    "available": available,
                    "bar_count": count,
                    "expected_count": expected,
                }
            )
            if not available:
                gaps.append(Gap(symbol, key, stream, count, expected))

    await upsert_chunked(
        StreamAvailability,
        rows,
        index_elements=[
            StreamAvailability.symbol,
            StreamAvailability.month,
            StreamAvailability.stream,
        ],
        update_columns=("available", "bar_count", "expected_count"),
    )

    return gaps


def summarize_gaps(gaps: list[Gap]) -> dict[str, int]:
    """Summary: how many months are entirely empty, how many merely incomplete."""
    absent = sum(1 for g in gaps if g.absent)
    return {
        "total": len(gaps),
        "absent": absent,
        "partial": len(gaps) - absent,
        "symbols": len({g.symbol for g in gaps}),
        "streams": len({g.stream for g in gaps}),
    }
