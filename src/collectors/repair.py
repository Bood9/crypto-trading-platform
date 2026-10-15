"""Targeted repair of gaps in collected series.

Needed because the checkpoint stores the bounds of the covered range, while
a gap lies INSIDE it: the window planner will not see it and will fetch nothing.
Re-downloading the whole series for 0.1% of the data costs hours per
timeframe.

Added after a finding: the collector lost exactly one bar at the edge of
every page (the window spanned 1001 slots with a limit of 1000). The bug
is fixed, but data already collected has to be repaired, not collected again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.collectors.ohlcv import TIMEFRAMES, OhlcvCollector
from src.collectors.validation import validate_ohlcv
from src.core.logging import get_logger
from src.exchange.bybit_client import BybitClient, BybitError

log = get_logger(__name__)

#: How many bars to grab around a gap. The margin is needed because the
#: exchange's window bounds are inclusive, and a request exactly the size
#: of the gap risks returning nothing.
PADDING_BARS = 3


@dataclass
class RepairResult:
    symbol: str
    timeframe: str
    gaps_found: int = 0
    bars_written: int = 0
    requests: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def repair_symbol(
    client: BybitClient,
    symbol: str,
    timeframe: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    max_gaps: int = 500,
) -> RepairResult:
    """Finds gaps and fetches the missing bars.

    Writes are idempotent (upsert), so grabbing neighbouring bars
    is harmless — they are simply overwritten with the same values.
    """
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"unknown timeframe {timeframe!r}")

    result = RepairResult(symbol=symbol, timeframe=timeframe)
    report = await validate_ohlcv(symbol, timeframe, start=start, end=end)
    if not report.gaps:
        return result

    result.gaps_found = len(report.gaps)
    collector = OhlcvCollector(client, timeframe)
    step = collector.interval

    # There may be many gaps; each one is a separate request. The cap
    # guards against the case where the "gap" turns out to be the whole series.
    for gap in report.gaps[:max_gaps]:
        window_start = gap.start - step * PADDING_BARS
        window_end = gap.end + step * PADDING_BARS
        try:
            rows, requests = await collector.fetch(symbol, window_start, window_end)
        except BybitError as exc:
            result.error = str(exc)
            log.warning(
                "repair.failed", symbol=symbol, timeframe=timeframe, error=str(exc)
            )
            return result

        result.requests += requests
        if rows:
            result.bars_written += await collector.store(rows)

    log.info(
        "repair.done",
        symbol=symbol,
        timeframe=timeframe,
        gaps=result.gaps_found,
        written=result.bars_written,
    )
    return result


async def verify_repair(
    symbol: str, timeframe: str, *, start: datetime | None = None, end: datetime | None = None
) -> tuple[int, int]:
    """Gaps before and after the repair.

    A separate check rather than trusting the returned row count:
    the bars written might not match the missing ones (P-002 —
    a result counts by fact, not by the operation's own report).
    """
    report = await validate_ohlcv(symbol, timeframe, start=start, end=end)
    return len(report.gaps), report.missing_bars


def gap_windows(
    gaps: list, step: timedelta, *, padding: int = PADDING_BARS
) -> list[tuple[datetime, datetime]]:
    """Request windows around gaps. Extracted for testability."""
    return [(g.start - step * padding, g.end + step * padding) for g in gaps]
