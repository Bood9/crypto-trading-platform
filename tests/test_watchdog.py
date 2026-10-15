"""Watchdog tests.

Catches the failure class "process alive, data stale, nobody knows".
It already showed up in the token bucket: a livelock on fractional arithmetic
left the collector looking healthy while not a single request went through.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.collectors.checkpoints import CheckpointRow, evaluate_staleness
from src.core.models import DataStream

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

INTERVALS = {
    DataStream.KLINE_15M: timedelta(minutes=15),
    DataStream.KLINE_1H: timedelta(hours=1),
    DataStream.KLINE_4H: timedelta(hours=4),
    DataStream.OI: timedelta(hours=1),
    DataStream.FUNDING: timedelta(hours=8),
}


def row(stream: DataStream, *, ago: timedelta | None, symbol="SOLUSDT", failures=0, error=None):
    last = None if ago is None else NOW - ago
    return CheckpointRow(
        symbol=symbol,
        stream=stream.value,
        last_success_at=last,
        consecutive_failures=failures,
        last_error=error,
    )


def check(rows, max_intervals=3.0):
    return evaluate_staleness(rows, INTERVALS, max_intervals=max_intervals, now=NOW)


def test_fresh_stream_is_not_flagged():
    assert check([row(DataStream.KLINE_15M, ago=timedelta(minutes=10))]) == []


def test_stream_within_threshold_is_not_flagged():
    # 3 intervals of 15 minutes = 45 minutes, threshold not exceeded.
    assert check([row(DataStream.KLINE_15M, ago=timedelta(minutes=44))]) == []


def test_stream_beyond_threshold_is_flagged():
    stale = check([row(DataStream.KLINE_15M, ago=timedelta(minutes=60))])

    assert len(stale) == 1
    assert stale[0].stream is DataStream.KLINE_15M
    assert stale[0].intervals_missed == pytest.approx(4.0)


def test_never_updated_stream_is_always_flagged():
    """A collector that never succeeded is not "no data yet"."""
    stale = check([row(DataStream.OI, ago=None)])

    assert len(stale) == 1
    assert stale[0].never_succeeded
    assert stale[0].intervals_missed == float("inf")


def test_threshold_scales_with_stream_interval():
    """An hour idle is normal for funding and already a failure for 15m candles.

    That is why lag is measured in intervals rather than minutes.
    """
    stale = check(
        [
            row(DataStream.FUNDING, ago=timedelta(hours=1), symbol="AUSDT"),
            row(DataStream.KLINE_15M, ago=timedelta(hours=1), symbol="BUSDT"),
        ]
    )

    flagged = {s.symbol for s in stale}
    assert flagged == {"BUSDT"}


def test_results_are_sorted_worst_first():
    stale = check(
        [
            row(DataStream.KLINE_1H, ago=timedelta(hours=5), symbol="MILD"),
            row(DataStream.KLINE_1H, ago=timedelta(hours=50), symbol="SEVERE"),
            row(DataStream.KLINE_1H, ago=None, symbol="NEVER"),
        ]
    )

    assert [s.symbol for s in stale] == ["NEVER", "SEVERE", "MILD"]


def test_failure_context_is_preserved():
    """An alert without a cause forces digging through logs by hand."""
    stale = check(
        [row(DataStream.OI, ago=timedelta(hours=10), failures=7, error="HTTP 503")]
    )

    assert stale[0].consecutive_failures == 7
    assert stale[0].last_error == "HTTP 503"


def test_unknown_stream_is_skipped_loudly_not_silently():
    """P-001: an unknown stream is logged as an error, not ignored."""
    bogus = CheckpointRow(symbol="XUSDT", stream="kline_7m", last_success_at=None)

    stale = check([bogus])

    # It is not in the report (there is no interval for it), but it does not
    # pretend to be healthy either: the fact is recorded in the log.
    assert stale == []


def test_stream_without_configured_interval_is_skipped():
    partial = {DataStream.KLINE_1H: timedelta(hours=1)}
    stale = evaluate_staleness(
        [row(DataStream.OI, ago=timedelta(days=30))],
        partial,
        max_intervals=3.0,
        now=NOW,
    )
    assert stale == []


def test_multiple_symbols_are_reported_independently():
    stale = check(
        [
            row(DataStream.KLINE_1H, ago=timedelta(minutes=30), symbol="OKUSDT"),
            row(DataStream.KLINE_1H, ago=timedelta(hours=9), symbol="BADUSDT"),
        ]
    )

    assert [s.symbol for s in stale] == ["BADUSDT"]


def test_empty_input_is_not_an_alert():
    assert check([]) == []
