"""Shared collector mechanics.

Invariant of every collector: writes are idempotent, the checkpoint moves
only after a successful write, and incomplete intervals are never
stored.

The last one is not an optimisation but lookahead protection. The candle
still forming contains the future relative to the decision moment: store
it, and the backtest sees the close of a bar that in reality has not
closed yet.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.collectors.checkpoints import get_checkpoint, record_failure, record_success
from src.core.logging import get_logger
from src.core.models import DataStream
from src.exchange.bybit_client import BybitCircuitOpen, BybitClient, BybitError

log = get_logger(__name__)


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_ms(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC)


def page_window_end(
    cursor: datetime, interval: timedelta, page_limit: int, hard_end: datetime
) -> datetime:
    """Right edge of a single page's window.

    **The only place this arithmetic lives.** Three collectors
    independently repeated the same mistake here, so a fourth has
    nowhere to repeat it.

    Subtlety: Bybit window bounds are INCLUSIVE on both sides.
    The interval `[cursor, cursor + N*interval]` spans N+1 slots,
    and with limit N the exchange returns the N NEWEST records — so the
    oldest one, exactly on the `cursor` edge, is dropped. The cursor then
    steps over it, and the record is lost for good.

    Hence exactly `page_limit - 1` steps, not `page_limit`.

    Found on candles: one bar lost per thousand, 0.1% of the data
    with a perfectly regular step. Neither the row counter nor the
    checkpoints showed it (P-004).
    """
    if page_limit < 1:
        raise ValueError(f"page size must be positive, got {page_limit}")
    if interval.total_seconds() <= 0:
        raise ValueError("interval must be positive")
    return min(cursor + interval * (page_limit - 1), hard_end)


def next_cursor(
    cursor: datetime,
    window_end: datetime,
    *,
    accepted: int,
    newest: datetime,
    interval: timedelta,
) -> datetime:
    """Where to move the cursor after a page.

    **The second place where window arithmetic must live alone** (DEC-004,
    DEC-029). Advancing was copied into three collectors, and all three
    alike crawled one bar at a time where they should jump a whole window.

    If nothing was accepted from the page — it is empty, or everything on
    it was dropped — the window is fully covered and the cursor jumps to
    its end. "Everything dropped" happens at the edge: Bybit's window end
    is INCLUSIVE, ours is exclusive, and when a backward window hits the
    first bar of history, the exchange returns exactly that edge bar and
    we drop it as `ts >= end`. The old "progress guarantee" moved the
    cursor by ONE interval, and the next request returned the same bar.
    Up to a thousand requests per young symbol — 957 requests for a
    single row in the measurement (P-004: regularity where there should
    be none).

    If something was accepted — continue right after the newest accepted.
    """
    if accepted == 0:
        # Guard against moving backwards: the window is never left of the
        # cursor, but if it were, rolling the cursor back would loop forever.
        return max(window_end, cursor + interval)
    return max(newest + interval, cursor + interval)


def dec(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


@dataclass
class CollectResult:
    symbol: str
    stream: DataStream
    rows_written: int = 0
    requests: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None
    error: str | None = None
    # The stream is entirely absent for the symbol (the MULTIUSDT case:
    # candles exist, OI and funding do not). Not a collection error but a
    # fact about the data.
    absent: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


class BaseCollector(abc.ABC):
    """Template for incremental collection of one stream for one symbol."""

    stream: DataStream
    #: Nominal step of the stream. Used by the watchdog and by the expected
    #: bar count in the availability matrix.
    interval: timedelta

    def __init__(self, client: BybitClient) -> None:
        self._client = client

    @abc.abstractmethod
    async def fetch(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[dict], int]:
        """Fetches a window of data. Returns the rows and the request count."""

    @abc.abstractmethod
    async def store(self, rows: list[dict]) -> int:
        """Writes rows idempotently. Returns how many."""

    def complete_before(self, now: datetime) -> datetime:
        """Boundary of completed intervals.

        A bar stamped T closes at T + interval, so only bars whose
        T + interval has already passed may be stored.
        """
        return now - self.interval

    def plan_windows(
        self,
        requested_start: datetime,
        requested_end: datetime,
        covered_first: datetime | None,
        covered_last: datetime | None,
    ) -> list[tuple[datetime, datetime]]:
        """Which windows remain to fetch, given the range already covered.

        Returns up to two windows: the missing past and the missing
        future. The already covered middle is not downloaded again.

        The past window is mandatory. Without it, a deeper request after
        a shallow run would do nothing and report "0 rows" — i.e. it
        would look like "everything collected". That is exactly what
        happened on the rehearsal: 18 days of data with a checkpoint at today.
        """
        if covered_first is None or covered_last is None:
            return [(requested_start, requested_end)] if requested_start < requested_end else []

        windows: list[tuple[datetime, datetime]] = []
        if requested_start < covered_first:
            windows.append((requested_start, covered_first))
        # From the next interval: the bar stamped covered_last is already written.
        forward_start = covered_last + self.interval
        if forward_start < requested_end:
            windows.append((forward_start, requested_end))
        return windows

    async def run(
        self,
        symbol: str,
        *,
        start: datetime,
        end: datetime | None = None,
        incremental: bool = True,
    ) -> CollectResult:
        now = datetime.now(UTC)
        window_end = min(end or now, self.complete_before(now))
        result = CollectResult(symbol=symbol, stream=self.stream)

        if incremental:
            checkpoint = await get_checkpoint(symbol, self.stream)
            windows = self.plan_windows(
                start,
                window_end,
                checkpoint.first_ts if checkpoint else None,
                checkpoint.last_ts if checkpoint else None,
            )
        else:
            windows = [(start, window_end)] if start < window_end else []

        if not windows:
            # Nothing to do — but that is success, not failure: touch the
            # checkpoint so the watchdog does not mistake a lull for a stall.
            await record_success(symbol, self.stream, None)
            return result

        if len(windows) > 1 or windows[0][0] < start + self.interval:
            log.info(
                "collector.windows",
                symbol=symbol,
                stream=self.stream.value,
                windows=[(a.isoformat(), b.isoformat()) for a, b in windows],
            )

        rows: list[dict] = []
        for win_start, win_end in windows:
            try:
                page, requests = await self.fetch(symbol, win_start, win_end)
            except BybitCircuitOpen:
                # The circuit breaker is not one symbol's failure but a
                # decision to stop work entirely. Recording it as an ordinary
                # error would mean continuing the sweep and finishing off the ban.
                await record_failure(symbol, self.stream, "circuit breaker open")
                raise
            except BybitError as exc:
                result.error = str(exc)
                await record_failure(symbol, self.stream, str(exc))
                log.warning(
                    "collector.failed",
                    symbol=symbol,
                    stream=self.stream.value,
                    error=str(exc),
                )
                return result
            result.requests += requests
            rows.extend(page)

        if not rows:
            # An empty window for a live symbol is normal (the exchange
            # returned no bar). Total absence of a stream is detected
            # separately, when building the availability matrix.
            await record_success(symbol, self.stream, None)
            return result

        timestamps = [r["ts"] for r in rows]
        result.first_ts = min(timestamps)
        result.last_ts = max(timestamps)
        result.rows_written = await self.store(rows)

        await record_success(symbol, self.stream, result.last_ts, result.first_ts)
        log.info(
            "collector.done",
            symbol=symbol,
            stream=self.stream.value,
            rows=result.rows_written,
            requests=result.requests,
            since=result.first_ts.isoformat(),
            through=result.last_ts.isoformat(),
        )
        return result
