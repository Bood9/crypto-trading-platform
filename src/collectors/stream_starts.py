"""Start dates of each stream per symbol, and the effective_start computation.

Stored separately per stream rather than as a single listing date:
for some pairs OI starts earlier than candles (ETHUSDT — OI from 2020-10,
candles from 2021-03). A single date would be wrong for at least one stream,
and the backtest would either skip available data or reach for data
that does not exist.

`effective_start` for a set of required streams is their maximum: a setup
can start working only once EVERYTHING it requires is available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select

from src.core.bulk import upsert_chunked
from src.core.db import (
    FundingHistory,
    Ohlcv,
    OpenInterest,
    StreamStartRow,
    session_scope,
)
from src.core.logging import get_logger
from src.core.models import DataStream
from src.strategy.universe import effective_start

log = get_logger(__name__)

_SOURCE = {
    DataStream.KLINE_15M: (Ohlcv, "15m"),
    DataStream.KLINE_1H: (Ohlcv, "1h"),
    DataStream.KLINE_4H: (Ohlcv, "4h"),
    DataStream.OI: (OpenInterest, None),
    DataStream.FUNDING: (FundingHistory, None),
}


@dataclass(frozen=True)
class SymbolStarts:
    symbol: str
    starts: dict[DataStream, datetime | None]
    ends: dict[DataStream, datetime | None]

    def effective_for(self, required: frozenset[DataStream]) -> datetime | None:
        return effective_start(self.starts, required)

    def missing(self, required: frozenset[DataStream]) -> list[DataStream]:
        """Streams that do not exist at all. The MULTIUSDT case."""
        return sorted(
            (s for s in required if self.starts.get(s) is None),
            key=lambda s: s.value,
        )


async def collect_starts(symbols: list[str]) -> dict[str, SymbolStarts]:
    """Bounds of every stream for every symbol, one pass per stream.

    Aggregation in Postgres: iterating symbols one by one would mean
    several hundred queries where five are enough.
    """
    starts: dict[str, dict[DataStream, datetime | None]] = {s: {} for s in symbols}
    ends: dict[str, dict[DataStream, datetime | None]] = {s: {} for s in symbols}

    async with session_scope() as session:
        for stream, (table, timeframe) in _SOURCE.items():
            query = select(
                table.symbol, func.min(table.ts), func.max(table.ts)
            ).where(table.symbol.in_(symbols))
            if timeframe is not None:
                query = query.where(table.timeframe == timeframe)
            if stream is DataStream.OI:
                query = query.where(table.interval == "1h")
            query = query.group_by(table.symbol)

            for symbol, first, last in (await session.execute(query)).all():
                if symbol in starts:
                    starts[symbol][stream] = first
                    ends[symbol][stream] = last

    return {
        s: SymbolStarts(symbol=s, starts=starts[s], ends=ends[s]) for s in symbols
    }


async def persist_starts(data: dict[str, SymbolStarts]) -> int:
    rows = [
        {
            "symbol": item.symbol,
            "stream": stream.value,
            "first_ts": item.starts.get(stream),
            "last_ts": item.ends.get(stream),
        }
        for item in data.values()
        for stream in DataStream
    ]
    await upsert_chunked(
        StreamStartRow,
        rows,
        index_elements=[StreamStartRow.symbol, StreamStartRow.stream],
        update_columns=("first_ts", "last_ts"),
    )
    log.info("stream_starts.persisted", rows=len(rows))
    return len(rows)


@dataclass
class SetupReadiness:
    """Readiness of the universe for a specific setup."""

    setup: str
    required: frozenset[DataStream]
    ready: list[tuple[str, datetime]]
    missing_streams: dict[str, list[DataStream]]
    no_data: list[str]
    #: Coverage exists formally, but within the period it is so full of
    #: holes that the stream is unusable. Filled separately: a stream's
    #: bounds say nothing about its completeness.
    too_sparse: dict[str, list[tuple[DataStream, float]]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return (
            len(self.ready)
            + len(self.missing_streams)
            + len(self.no_data)
            + len(self.too_sparse)
        )

    @property
    def lost_to_single_stream(self) -> dict[str, DataStream]:
        """Symbols dropped because of EXACTLY ONE missing stream.

        More important than the total of unusable ones: a symbol with full
        candles but no OI is lost entirely, although it has almost all data.
        If there are many such, the actual width of the universe is noticeably
        below nominal, and that must be known before the run, not from an
        inexplicably low trade count.
        """
        return {
            symbol: missing[0]
            for symbol, missing in self.missing_streams.items()
            if len(missing) == 1
        }

    def single_stream_breakdown(self) -> dict[DataStream, int]:
        """Which stream most often turns out to be the only obstacle."""
        counts: dict[DataStream, int] = {}
        for stream in self.lost_to_single_stream.values():
            counts[stream] = counts.get(stream, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def summary(self) -> dict[str, int]:
        return {
            "ready": len(self.ready),
            "missing stream": len(self.missing_streams),
            "  of which due to one stream": len(self.lost_to_single_stream),
            "coverage too sparse": len(self.too_sparse),
            "no data at all": len(self.no_data),
        }


async def compute_coverage(
    symbols: list[str], streams: frozenset[DataStream], start: datetime, end: datetime
) -> dict[tuple[str, DataStream], float]:
    """Density of a stream WITHIN its own range.

    Answers "is the series full of holes", not "does it cover the period".
    These are two different quantities and must not be mixed:

    * low period coverage with a dense series of its own — a young pair.
      A routine case, handled by the short-symbol policy
      (`backtest.short_history`), not by the integrity check.
    * low density within its own range — real holes,
      the series is unusable for indicators.

    The first version computed the share of the period and would have
    rejected 49 perfectly healthy symbols, each with an own-series density
    of 100%. Measured on the collected data: symbols with real holes
    total ZERO.
    """
    from src.collectors.coverage import STREAM_INTERVAL

    out: dict[tuple[str, DataStream], float] = {}

    async with session_scope() as session:
        for stream in streams:
            table, timeframe = _SOURCE[stream]
            step = STREAM_INTERVAL[stream].total_seconds()

            query = select(
                table.symbol, func.count(), func.min(table.ts), func.max(table.ts)
            ).where(table.symbol.in_(symbols), table.ts >= start, table.ts < end)
            if timeframe is not None:
                query = query.where(table.timeframe == timeframe)
            if stream is DataStream.OI:
                query = query.where(table.interval == "1h")
            query = query.group_by(table.symbol)

            stats = {
                symbol: (count, first, last)
                for symbol, count, first, last in (await session.execute(query)).all()
            }

            for symbol in symbols:
                entry = stats.get(symbol)
                if entry is None:
                    # The stream does not exist at all — that is not "zero
                    # density" but absence, and it is handled in `missing`.
                    out[(symbol, stream)] = 0.0
                    continue
                count, first, last = entry
                own_slots = max(1.0, (last - first).total_seconds() / step + 1)
                out[(symbol, stream)] = min(1.0, count / own_slots)

    return out


def default_min_coverage() -> float:
    """Coverage threshold from the config (DEC-012).

    Read from there, not hardcoded: the value was fixed before the run
    alongside the acceptance thresholds, and duplicating it in code would
    create a second source of truth that would one day diverge from the first.
    """
    from src.core.config import get_settings

    return get_settings().data.min_stream_coverage


def assess_readiness(
    data: dict[str, SymbolStarts],
    setup: str,
    required: frozenset[DataStream],
    coverage: dict[tuple[str, DataStream], float] | None = None,
    *,
    min_coverage: float | None = None,
) -> SetupReadiness:
    """Which symbols are usable for the setup, and from what moment.

    Separates two cases that would otherwise merge into "unusable":
    the symbol has NO streams at all (it simply was not downloaded) versus
    the symbol lacks a SPECIFIC stream (the MULTIUSDT case). These are
    different problems, and they are fixed differently.
    """
    threshold = default_min_coverage() if min_coverage is None else min_coverage

    ready: list[tuple[str, datetime]] = []
    missing: dict[str, list[DataStream]] = {}
    no_data: list[str] = []
    sparse: dict[str, list[tuple[DataStream, float]]] = {}

    for symbol, item in sorted(data.items()):
        if all(v is None for v in item.starts.values()):
            no_data.append(symbol)
            continue

        start = item.effective_for(required)
        if start is None:
            missing[symbol] = item.missing(required)
            continue

        # The stream is there, but how complete it is within the period
        # is a separate question that the bounds do not answer.
        if coverage is not None:
            thin = [
                (stream, coverage.get((symbol, stream), 0.0))
                for stream in sorted(required, key=lambda s: s.value)
                if coverage.get((symbol, stream), 0.0) < threshold
            ]
            if thin:
                sparse[symbol] = thin
                continue

        ready.append((symbol, start))

    return SetupReadiness(
        setup=setup,
        required=required,
        ready=ready,
        missing_streams=missing,
        no_data=no_data,
        too_sparse=sparse,
    )
