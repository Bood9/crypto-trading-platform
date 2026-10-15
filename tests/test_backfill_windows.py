"""Tests for backfill window planning.

Regression from a rehearsal: the checkpoint stored only the forward edge,
so a two-year request after a shallow run silently did nothing
and reported "0 rows". The DB held 18 days of history while the
checkpoint pointed at today — so the report looked like "all collected".

Resuming must work in both directions: fetch fresh data
and deepen history, without re-downloading the already covered middle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.collectors.ohlcv import OhlcvCollector

HOUR = timedelta(hours=1)


def collector() -> OhlcvCollector:
    return OhlcvCollector(client=None, timeframe="1h")


def t(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


def test_empty_checkpoint_fetches_whole_window():
    windows = collector().plan_windows(t(1), t(10), None, None)
    assert windows == [(t(1), t(10))]


def test_forward_only_when_history_is_deep_enough():
    """A normal incremental run: fetch fresh data."""
    windows = collector().plan_windows(t(1), t(10), covered_first=t(1), covered_last=t(8))
    assert windows == [(t(8) + HOUR, t(10))]


def test_backward_window_is_added_when_deeper_history_requested():
    """The exact case: a shallow run, then a request for greater depth."""
    windows = collector().plan_windows(t(1), t(10), covered_first=t(6), covered_last=t(10))
    assert windows == [(t(1), t(6))]


def test_both_windows_when_gaps_on_both_sides():
    windows = collector().plan_windows(t(1), t(20), covered_first=t(5), covered_last=t(10))
    assert windows == [(t(1), t(5)), (t(10) + HOUR, t(20))]


def test_covered_middle_is_never_refetched():
    """Re-downloading what is already collected is explicitly forbidden."""
    windows = collector().plan_windows(t(1), t(20), covered_first=t(5), covered_last=t(10))
    for start, end in windows:
        assert not (t(5) < start < t(10)), "window overlaps the covered range"
        assert not (t(5) < end < t(10)), "window overlaps the covered range"


def test_nothing_to_do_when_fully_covered():
    windows = collector().plan_windows(t(1), t(10), covered_first=t(1), covered_last=t(10))
    assert windows == []


def test_nothing_to_do_when_coverage_exceeds_request():
    windows = collector().plan_windows(t(5), t(8), covered_first=t(1), covered_last=t(10))
    assert windows == []


def test_empty_request_window_yields_nothing():
    assert collector().plan_windows(t(10), t(10), None, None) == []
    assert collector().plan_windows(t(10), t(5), None, None) == []


def test_forward_window_starts_after_last_covered_bar():
    """The bar stamped covered_last is already written — no need to repeat it."""
    windows = collector().plan_windows(t(1), t(10), covered_first=t(1), covered_last=t(8))
    assert windows[0][0] == t(8) + HOUR


@pytest.mark.parametrize("timeframe,step", [("15m", 15), ("1h", 60), ("4h", 240)])
def test_forward_offset_matches_timeframe(timeframe, step):
    coll = OhlcvCollector(client=None, timeframe=timeframe)
    windows = coll.plan_windows(t(1), t(10), covered_first=t(1), covered_last=t(8))
    assert windows[0][0] == t(8) + timedelta(minutes=step)


def test_half_open_checkpoint_is_treated_as_absent():
    """One edge without the other is useless: it does not define a range."""
    coll = collector()
    assert coll.plan_windows(t(1), t(10), covered_first=t(5), covered_last=None) == [
        (t(1), t(10))
    ]
    assert coll.plan_windows(t(1), t(10), covered_first=None, covered_last=t(5)) == [
        (t(1), t(10))
    ]
