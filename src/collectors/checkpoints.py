"""Collector checkpoints and the watchdog.

The watchdog answers a question nobody else asks: is the collector
not updating because there is no data, or because it silently stalled?
Without `last_success_at` these two states are indistinguishable, and a
failure looks like silence.

The same class of failure already showed up in the token bucket (a livelock
on fractional arithmetic): the process is alive, the data is stale, the
process metrics look fine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from src.core.db import CollectorCheckpoint, session_scope
from src.core.logging import get_logger
from src.core.models import DataStream

log = get_logger(__name__)


@dataclass(frozen=True)
class Checkpoint:
    """The range already covered and the state of the last pass.

    The range keeps both bounds: the front one alone is not enough,
    otherwise deepening history after a shallow run silently does nothing.
    """

    symbol: str
    stream: DataStream
    first_ts: datetime | None
    last_ts: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int


async def get_checkpoint(symbol: str, stream: DataStream) -> Checkpoint | None:
    async with session_scope() as session:
        row = await session.get(CollectorCheckpoint, (symbol, stream.value))
        if row is None:
            return None
        return Checkpoint(
            symbol=row.symbol,
            stream=DataStream(row.stream),
            first_ts=row.first_ts,
            last_ts=row.last_ts,
            last_success_at=row.last_success_at,
            consecutive_failures=row.consecutive_failures,
        )


async def record_success(
    symbol: str,
    stream: DataStream,
    last_ts: datetime | None,
    first_ts: datetime | None = None,
) -> None:
    """Records a successful pass and widens the covered range.

    Bounds only move outwards: `last_ts` forward, `first_ts` backward.
    Re-running an earlier window (backfilling a gap) must not move the
    front bound into the past and make the collector download again
    what it already has; symmetrically, loading fresh data must not
    "forget" the depth already collected.
    """
    now = datetime.now(UTC)
    values = {
        "symbol": symbol,
        "stream": stream.value,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "last_success_at": now,
        "last_attempt_at": now,
        "consecutive_failures": 0,
        "last_error": None,
    }
    stmt = insert(CollectorCheckpoint).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[CollectorCheckpoint.symbol, CollectorCheckpoint.stream],
        set_={
            # GREATEST ignores NULL, so the first pass
            # (no old value yet) works without a special case.
            "last_ts": func.greatest(stmt.excluded.last_ts, CollectorCheckpoint.last_ts),
            # LEAST/GREATEST in Postgres ignore NULL, so the first
            # pass works without a special case.
            "first_ts": func.least(stmt.excluded.first_ts, CollectorCheckpoint.first_ts),
            "last_success_at": stmt.excluded.last_success_at,
            "last_attempt_at": stmt.excluded.last_attempt_at,
            "consecutive_failures": 0,
            "last_error": None,
        },
    )
    async with session_scope() as session:
        await session.execute(stmt)


async def record_failure(symbol: str, stream: DataStream, error: str) -> None:
    """Records a failure WITHOUT moving `last_ts`.

    Moving the bound on error would mean silently losing a window of data:
    the next pass would start from a point it never reached.
    """
    now = datetime.now(UTC)
    stmt = insert(CollectorCheckpoint).values(
        symbol=symbol,
        stream=stream.value,
        first_ts=None,
        last_ts=None,
        last_attempt_at=now,
        consecutive_failures=1,
        last_error=error[:2000],
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[CollectorCheckpoint.symbol, CollectorCheckpoint.stream],
        set_={
            "last_attempt_at": stmt.excluded.last_attempt_at,
            "consecutive_failures": CollectorCheckpoint.consecutive_failures + 1,
            "last_error": stmt.excluded.last_error,
        },
    )
    async with session_scope() as session:
        await session.execute(stmt)


@dataclass(frozen=True)
class StaleStream:
    symbol: str
    stream: DataStream
    last_success_at: datetime | None
    age: timedelta | None
    intervals_missed: float
    consecutive_failures: int
    last_error: str | None

    @property
    def never_succeeded(self) -> bool:
        return self.last_success_at is None


@dataclass(frozen=True)
class CheckpointRow:
    """Snapshot of a checkpoint row. Decouples the watchdog logic from the ORM."""

    symbol: str
    stream: str
    last_success_at: datetime | None
    consecutive_failures: int = 0
    last_error: str | None = None


def evaluate_staleness(
    rows: list[CheckpointRow],
    expected_interval: dict[DataStream, timedelta],
    *,
    max_intervals: float,
    now: datetime,
) -> list[StaleStream]:
    """Pure part of the watchdog: which streams count as stale.

    Lag is measured in stream intervals, not in absolute time.
    Otherwise 15-minute candles and 8-hour funding would need different
    thresholds, and any config change would make them diverge in meaning.

    A stream that has NEVER updated is always stale: it is either a
    collector that has never run, or a symbol for which the stream is
    absent. Both cases call for attention, not silence (P-001).
    """
    stale: list[StaleStream] = []

    for row in rows:
        try:
            stream = DataStream(row.stream)
        except ValueError:
            # P-001: an unknown stream is no reason to stay silent.
            log.error("watchdog.unknown_stream", symbol=row.symbol, stream=row.stream)
            continue

        interval = expected_interval.get(stream)
        if interval is None or interval.total_seconds() <= 0:
            log.error("watchdog.no_interval", symbol=row.symbol, stream=row.stream)
            continue

        if row.last_success_at is None:
            stale.append(
                StaleStream(
                    symbol=row.symbol,
                    stream=stream,
                    last_success_at=None,
                    age=None,
                    intervals_missed=float("inf"),
                    consecutive_failures=row.consecutive_failures,
                    last_error=row.last_error,
                )
            )
            continue

        age = now - row.last_success_at
        missed = age.total_seconds() / interval.total_seconds()
        if missed > max_intervals:
            stale.append(
                StaleStream(
                    symbol=row.symbol,
                    stream=stream,
                    last_success_at=row.last_success_at,
                    age=age,
                    intervals_missed=missed,
                    consecutive_failures=row.consecutive_failures,
                    last_error=row.last_error,
                )
            )

    stale.sort(key=lambda s: s.intervals_missed, reverse=True)
    return stale


async def find_stale(
    expected_interval: dict[DataStream, timedelta],
    *,
    max_intervals: float,
    now: datetime | None = None,
) -> list[StaleStream]:
    """Streams that have not had a successful update for a long time."""
    async with session_scope() as session:
        rows = (await session.execute(select(CollectorCheckpoint))).scalars().all()

    snapshot = [
        CheckpointRow(
            symbol=r.symbol,
            stream=r.stream,
            last_success_at=r.last_success_at,
            consecutive_failures=r.consecutive_failures,
            last_error=r.last_error,
        )
        for r in rows
    ]
    return evaluate_staleness(
        snapshot,
        expected_interval,
        max_intervals=max_intervals,
        now=now or datetime.now(UTC),
    )
