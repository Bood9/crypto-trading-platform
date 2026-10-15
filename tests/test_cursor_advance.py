"""Advancing the pagination cursor. DEC-004, DEC-029.

Three collectors identically crawled one bar at a time at the history edge:
Bybit's window end is inclusive, ours is exclusive, and the boundary bar
returned by the exchange was dropped while the cursor moved by one interval.
Measured: 957 requests for a single row.

The test reproduces the exchange's behaviour, not the collector internals:
the fake client returns bars INCLUSIVELY at both ends of the window,
like the real one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.collectors.base import next_cursor
from src.collectors.ohlcv import OhlcvCollector

T0 = datetime(2021, 1, 1, tzinfo=UTC)
H4 = timedelta(hours=4)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


class InclusiveKlineClient:
    """Returns bars in [start, end] inclusive, newest first,
    at most limit — like Bybit /v5/market/kline."""

    def __init__(self, first_bar: datetime, last_bar: datetime) -> None:
        self.first = first_bar
        self.last = last_bar
        self.calls = 0

    async def get_kline(self, symbol, interval, *, start, end, limit):  # noqa: ANN001
        self.calls += 1
        lo = max(start, ms(self.first))
        hi = min(end, ms(self.last))
        if lo > hi:
            return []
        step = int(H4.total_seconds() * 1000)
        first_slot = ((lo - ms(self.first) + step - 1) // step) * step + ms(self.first)
        stamps = list(range(first_slot, hi + 1, step))
        stamps = stamps[-limit:]
        return [[str(t), "1", "1", "1", "1", "1", "1"] for t in reversed(stamps)]


def collector(client) -> OhlcvCollector:  # noqa: ANN001
    return OhlcvCollector(client, "4h")


# ---------------------------------------------------------------------------
# The advance function
# ---------------------------------------------------------------------------
def test_nothing_accepted_jumps_to_the_end_of_the_window():
    """This used to shift by one interval."""
    end = T0 + H4 * 999
    assert next_cursor(T0, end, accepted=0, newest=T0, interval=H4) == end


def test_something_accepted_continues_after_the_newest():
    newest = T0 + H4 * 10
    assert next_cursor(
        T0, T0 + H4 * 999, accepted=5, newest=newest, interval=H4
    ) == newest + H4


def test_cursor_never_moves_backwards():
    """A window left of the cursor would loop the traversal."""
    back = T0 - H4 * 5
    assert next_cursor(T0, back, accepted=0, newest=T0, interval=H4) > T0


# ---------------------------------------------------------------------------
# Collector against an exchange with an inclusive window end
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_boundary_bar_does_not_cause_a_crawl():
    """The backward window runs into the first bar of history.

    The exchange returns exactly the boundary bar — it sits on the exclusive end
    of our window and is dropped. This used to cost up to a thousand requests;
    now — as many as there are windows in the range, plus one.
    """
    listing = T0 + timedelta(days=900)
    client = InclusiveKlineClient(first_bar=listing, last_bar=listing + H4 * 50)

    rows, requests = await collector(client).fetch("TESTUSDT", T0, listing)

    assert rows == []
    windows = (listing - T0) / (H4 * 999)
    assert requests <= int(windows) + 3, f"{requests} requests for an empty range"


@pytest.mark.asyncio
async def test_all_bars_are_still_collected():
    """The fix must not lose anything: every bar in the range
    must land in the sample exactly once."""
    listing = T0 + timedelta(days=30)
    last = listing + H4 * 2500
    client = InclusiveKlineClient(first_bar=listing, last_bar=last)

    rows, _ = await collector(client).fetch("TESTUSDT", T0, last + H4)

    stamps = [r["ts"] for r in rows]
    assert len(stamps) == len(set(stamps)), "bars were duplicated"
    assert len(stamps) == 2501
    assert min(stamps) == listing
    assert max(stamps) == last


@pytest.mark.asyncio
async def test_exclusive_end_is_respected():
    listing = T0
    client = InclusiveKlineClient(first_bar=listing, last_bar=listing + H4 * 100)
    stop = listing + H4 * 40

    rows, _ = await collector(client).fetch("TESTUSDT", T0, stop)
    assert max(r["ts"] for r in rows) < stop
    assert len(rows) == 40


# ---------------------------------------------------------------------------
# Guard: the advance is not computed in place
# ---------------------------------------------------------------------------
def test_all_collectors_use_the_shared_cursor_advance():
    """Guard against a fourth collector with the same bug.

    The advance was copied into three places and identically broken
    in all three — exactly the story that already happened with the window boundary
    in DEC-004.
    """
    import inspect

    from src.collectors import funding, ohlcv, open_interest

    for module in (ohlcv, funding, open_interest):
        source = inspect.getsource(module)
        assert "next_cursor(" in source, f"{module.__name__} does not use the shared function"
        assert "cursor = max(newest" not in source, (
            f"{module.__name__} moves the cursor in place"
        )
