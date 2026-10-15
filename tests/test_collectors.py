"""Collector tests on a mock exchange.

The DB is mocked: what is checked is the window traversal logic and the save boundary,
not SQL. Writing is covered separately by an integration run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.collectors.base import BaseCollector, from_ms, ms
from src.collectors.funding import FundingCollector, normalize_to_8h
from src.collectors.ohlcv import PAGE_LIMIT, OhlcvCollector
from src.collectors.open_interest import OpenInterestCollector
from src.core.models import DataStream

D = Decimal


def dt(y, m, d, h=0, mi=0) -> datetime:
    return datetime(y, m, d, h, mi, tzinfo=UTC)


class FakeBybit:
    """Mock exchange: generates bars for the requested window."""

    def __init__(self, *, available_from: datetime | None = None, gap: tuple | None = None):
        self.available_from = available_from
        self.gap = gap  # (start, end) of a window without data
        self.calls: list[tuple] = []

    def _bars(self, start_ms, end_ms, step: timedelta):
        start, end = from_ms(start_ms), from_ms(end_ms)
        out = []
        # As on the exchange: timestamps sit on a fixed step grid rather than
        # being counted from the request start. Otherwise different pages would give
        # different "schedules", and inferring the interval from neighbouring records
        # would be tested on data that never occurs (DEC-036).
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        offset = (start - epoch) % step
        t = start if not offset else start + (step - offset)
        while t < end:
            skip = self.available_from is not None and t < self.available_from
            if self.gap and self.gap[0] <= t < self.gap[1]:
                skip = True
            if not skip:
                out.append(t)
            t += step
        return out

    async def get_kline(self, symbol, interval, *, start, end, limit=1000, category=None):
        self.calls.append(("kline", symbol, start, end))
        step = {"15": timedelta(minutes=15), "60": timedelta(hours=1)}[interval]
        ts = self._bars(start, end, step)[:limit]
        # Bybit returns newest to oldest.
        return [
            [str(ms(t)), "100", "110", "90", "105", "1000", "105000"] for t in reversed(ts)
        ]

    async def get_funding_history(self, symbol, *, start, end, limit=200, category=None):
        self.calls.append(("funding", symbol, start, end))
        # Like the real Bybit: on truncation — the NEWEST `limit` records.
        # The fake returned the oldest, and losing the older records
        # in an overflowing window went uncaught by the tests (DEC-036).
        ts = self._bars(start, end, timedelta(hours=8))[-limit:]
        return [
            {"symbol": symbol, "fundingRate": "0.0001", "fundingRateTimestamp": str(ms(t))}
            for t in reversed(ts)
        ]

    async def get_open_interest(
        self, symbol, interval="1h", *, start, end, limit=200, category=None
    ):
        self.calls.append(("oi", symbol, start, end))
        ts = self._bars(start, end, timedelta(hours=1))[:limit]
        return [
            {"openInterest": "5000.5", "timestamp": str(ms(t))} for t in reversed(ts)
        ]


# ---------------------------------------------------------------------------
# The boundary of completed intervals — protection against lookahead
# ---------------------------------------------------------------------------
def test_forming_candle_is_never_stored():
    """The current bar contains the future relative to the decision moment.

    By saving it, the backtest would see the close price of a bar that
    has not actually closed yet.
    """
    collector = OhlcvCollector(FakeBybit(), "15m")
    now = dt(2026, 8, 7, 12, 7)  # 7 minutes into the current bar

    boundary = collector.complete_before(now)

    assert boundary == dt(2026, 8, 7, 11, 52)
    assert boundary < dt(2026, 8, 7, 12, 0)


@pytest.mark.parametrize(
    "timeframe,expected",
    [("15m", timedelta(minutes=15)), ("1h", timedelta(hours=1)), ("4h", timedelta(hours=4))],
)
def test_interval_matches_timeframe(timeframe, expected):
    assert OhlcvCollector(FakeBybit(), timeframe).interval == expected


def test_unknown_timeframe_is_rejected():
    """P-001: do not substitute the nearest one."""
    with pytest.raises(ValueError, match="unknown timeframe"):
        OhlcvCollector(FakeBybit(), "3m")


def test_unknown_oi_interval_is_rejected():
    with pytest.raises(ValueError, match="unknown OI interval"):
        OpenInterestCollector(FakeBybit(), "7min")


# ---------------------------------------------------------------------------
# Window traversal
# ---------------------------------------------------------------------------
async def test_fetch_covers_whole_range_without_duplicates():
    api = FakeBybit()
    collector = OhlcvCollector(api, "1h")
    start, end = dt(2026, 1, 1), dt(2026, 1, 8)

    rows, requests = await collector.fetch("SOLUSDT", start, end)

    timestamps = [r["ts"] for r in rows]
    assert len(timestamps) == len(set(timestamps)), "duplicate bars"
    assert min(timestamps) == start
    assert max(timestamps) == end - timedelta(hours=1)
    assert len(timestamps) == 7 * 24
    assert requests >= 1


async def test_fetch_pages_through_more_than_one_window():
    """1000 bars per page: two years of 1h require many windows."""
    api = FakeBybit()
    collector = OhlcvCollector(api, "1h")
    start, end = dt(2026, 1, 1), dt(2026, 4, 1)

    rows, requests = await collector.fetch("SOLUSDT", start, end)

    expected_bars = int((end - start).total_seconds() // 3600)
    assert len(rows) == expected_bars
    assert requests >= expected_bars // PAGE_LIMIT


async def test_end_boundary_is_exclusive():
    api = FakeBybit()
    collector = OhlcvCollector(api, "1h")
    end = dt(2026, 1, 2)

    rows, _ = await collector.fetch("SOLUSDT", dt(2026, 1, 1), end)

    assert all(r["ts"] < end for r in rows)


async def test_gap_in_history_does_not_stop_collection():
    """An empty window is not the end of the data.

    The symbol might have been delisted and listed again. By stopping the traversal
    at the first gap, we would lose everything beyond it.
    """
    api = FakeBybit(gap=(dt(2026, 1, 3), dt(2026, 1, 5)))
    collector = OhlcvCollector(api, "1h")

    rows, _ = await collector.fetch("SOLUSDT", dt(2026, 1, 1), dt(2026, 1, 8))

    timestamps = {r["ts"] for r in rows}
    assert dt(2026, 1, 2) in timestamps, "data before the gap"
    assert dt(2026, 1, 6) in timestamps, "data AFTER the gap"
    assert dt(2026, 1, 4) not in timestamps, "the gap itself"


async def test_symbol_listed_later_yields_only_available_range():
    api = FakeBybit(available_from=dt(2026, 1, 5))
    collector = OhlcvCollector(api, "1h")

    rows, _ = await collector.fetch("HYPEUSDT", dt(2026, 1, 1), dt(2026, 1, 8))

    assert min(r["ts"] for r in rows) == dt(2026, 1, 5)


async def test_empty_response_everywhere_returns_nothing():
    """The stream is entirely absent for the symbol — the MULTIUSDT case."""
    api = FakeBybit(available_from=dt(2030, 1, 1))
    collector = OpenInterestCollector(api, "1h")

    rows, requests = await collector.fetch("MULTIUSDT", dt(2026, 1, 1), dt(2026, 1, 8))

    assert rows == []
    assert requests > 0


async def test_fetch_terminates_when_api_repeats_data():
    """A response consisting only of already seen bars must not loop the traversal."""

    class StuckApi(FakeBybit):
        async def get_kline(self, symbol, interval, *, start, end, limit=1000, category=None):
            self.calls.append(("kline", symbol, start, end))
            fixed = dt(2026, 1, 1)
            return [[str(ms(fixed)), "1", "1", "1", "1", "1", "1"]]

    collector = OhlcvCollector(StuckApi(), "1h")
    rows, requests = await collector.fetch("XUSDT", dt(2026, 1, 1), dt(2026, 1, 3))

    assert len(rows) == 1
    assert requests < 100, "traversal did not finish in a reasonable number of requests"


# ---------------------------------------------------------------------------
# Funding: normalization
# ---------------------------------------------------------------------------
def test_eight_hour_rate_is_unchanged():
    assert normalize_to_8h(D("0.0001"), 480) == D("0.0001")


def test_hourly_rate_scales_up_to_eight_hours():
    """An hourly rate is equivalent to eight times that over 8 hours."""
    assert normalize_to_8h(D("0.0001"), 60) == D("0.0008")


def test_four_hour_rate_doubles():
    assert normalize_to_8h(D("0.0001"), 240) == D("0.0002")


def test_negative_rate_keeps_sign():
    assert normalize_to_8h(D("-0.0001"), 60) == D("-0.0008")


def test_normalization_makes_symbols_comparable():
    """The point of normalization: raw rates of different intervals are not comparable.

    An hourly 0.0001 is "more expensive" than an eight-hour 0.0004, though it looks smaller.
    """
    hourly_raw, eight_hour_raw = D("0.0001"), D("0.0004")
    assert hourly_raw < eight_hour_raw
    assert normalize_to_8h(hourly_raw, 60) > normalize_to_8h(eight_hour_raw, 480)


def test_zero_interval_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        normalize_to_8h(D("0.0001"), 0)


async def test_funding_interval_comes_from_the_data_not_the_declaration():
    """The symbol is declared hourly, but the data has accruals every 8 hours.

    The previous test required stamping the declared 60 minutes against the
    data — i.e. it locked in the DEC-025 defect.
    """
    collector = FundingCollector(FakeBybit(), interval_minutes=60)
    rows, _ = await collector.fetch("SOLUSDT", dt(2026, 1, 1), dt(2026, 1, 3))

    assert rows
    for row in rows:
        assert row["interval_minutes"] == 480
        assert row["rate_8h"] == normalize_to_8h(row["rate"], 480)


# ---------------------------------------------------------------------------
# Resumption
# ---------------------------------------------------------------------------
class StubCollector(BaseCollector):
    stream = DataStream.KLINE_1H
    interval = timedelta(hours=1)

    async def fetch(self, symbol, start, end):
        return [], 0

    async def store(self, rows):
        return 0


class RecordingCollector(StubCollector):
    """Remembers the windows it was asked for."""

    def __init__(self, client) -> None:
        super().__init__(client)
        self.asked: list[tuple] = []

    async def fetch(self, symbol, start, end):
        self.asked.append((start, end))
        return [], 0


def _patch_checkpoint(monkeypatch, first, last):
    from src.collectors import base
    from src.collectors.checkpoints import Checkpoint

    async def fake(symbol, stream):
        if first is None and last is None:
            return None
        return Checkpoint(symbol, stream, first, last, last, 0)

    monkeypatch.setattr(base, "get_checkpoint", fake)
    monkeypatch.setattr(base, "record_success", _noop)
    monkeypatch.setattr(base, "record_failure", _noop)


async def _noop(*args, **kwargs):
    return None


async def test_run_resumes_after_last_stored_bar(monkeypatch):
    """The bar stamped last_ts is already written — continue from the next one."""
    last = dt(2026, 1, 5, 10)
    _patch_checkpoint(monkeypatch, dt(2024, 8, 1), last)

    collector = RecordingCollector(FakeBybit())
    await collector.run("SOLUSDT", start=dt(2024, 8, 1), end=dt(2026, 1, 10))

    assert collector.asked[0][0] == last + timedelta(hours=1)


async def test_run_fetches_everything_without_checkpoint(monkeypatch):
    _patch_checkpoint(monkeypatch, None, None)

    collector = RecordingCollector(FakeBybit())
    default = dt(2024, 8, 1)
    await collector.run("SOLUSDT", start=default, end=dt(2024, 9, 1))

    assert collector.asked[0][0] == default


async def test_run_deepens_history_when_more_is_requested(monkeypatch):
    """Rehearsal regression.

    A shallow backfill left the checkpoint at today, and a two-year request
    silently did nothing — "0 rows" read as "all collected".
    """
    _patch_checkpoint(monkeypatch, dt(2026, 7, 20), dt(2026, 8, 7))

    collector = RecordingCollector(FakeBybit())
    await collector.run("SOLUSDT", start=dt(2024, 8, 7), end=dt(2026, 8, 7))

    assert collector.asked, "the collector did not request a single window"
    assert collector.asked[0] == (dt(2024, 8, 7), dt(2026, 7, 20))


# ---------------------------------------------------------------------------
# Regression: losing a bar at the page boundary
# ---------------------------------------------------------------------------
class PagingBybit:
    """Imitates Bybit's behaviour: returns at most `limit` bars,
    newest to oldest, dropping the oldest on overflow."""

    def __init__(self, step_minutes: int, first: datetime, count: int) -> None:
        self.step = timedelta(minutes=step_minutes)
        self.bars = [first + self.step * i for i in range(count)]
        self.requests: list[tuple] = []

    async def get_kline(self, symbol, interval, *, start, end, limit):
        self.requests.append((start, end, limit))
        lo = datetime.fromtimestamp(start / 1000, UTC)
        hi = datetime.fromtimestamp(end / 1000, UTC)
        window = [b for b in self.bars if lo <= b <= hi]
        # Key point: the exchange returns the NEWEST limit bars.
        window = sorted(window)[-limit:]
        return [
            [str(int(b.timestamp() * 1000)), "1", "2", "0.5", "1", "10", "10"]
            for b in reversed(window)
        ]


async def test_no_bar_is_lost_at_page_boundary():
    """Regression for a found bug.

    The window [cursor, cursor + N*interval] covers N+1 slots: both
    bounds inclusive. With a limit of N the exchange returned the N newest
    bars, the oldest one was lost, and then the cursor stepped
    over it.

    The loss was exactly one bar per page for every symbol —
    0.1% of the data with a perfectly regular step.
    """
    from src.collectors.ohlcv import PAGE_LIMIT, OhlcvCollector

    first = dt(2024, 8, 1)
    total = PAGE_LIMIT * 3 + 137
    fake = PagingBybit(15, first, total)

    collector = OhlcvCollector(fake, "15m")
    rows, _ = await collector.fetch(
        "SOLUSDT", first, first + timedelta(minutes=15) * total
    )

    got = sorted(r["ts"] for r in rows)
    expected = [first + timedelta(minutes=15) * i for i in range(len(got))]

    assert got == expected, "the series is not continuous"
    # Not a single gap between neighbouring bars.
    steps = {b - a for a, b in zip(got, got[1:], strict=False)}
    assert steps == {timedelta(minutes=15)}, f"gaps found: {steps}"


async def test_page_window_spans_exactly_the_limit():
    """The window must cover exactly PAGE_LIMIT slots, no more."""
    from src.collectors.ohlcv import PAGE_LIMIT, OhlcvCollector

    first = dt(2024, 8, 1)
    fake = PagingBybit(15, first, PAGE_LIMIT * 2)
    collector = OhlcvCollector(fake, "15m")
    await collector.fetch("SOLUSDT", first, first + timedelta(minutes=15) * PAGE_LIMIT * 2)

    start_ms, end_ms, limit = fake.requests[0]
    slots = (end_ms - start_ms) / (15 * 60 * 1000) + 1
    assert slots == limit, f"window of {slots} slots with limit {limit}"


# ---------------------------------------------------------------------------
# The single place where the window arithmetic lives
# ---------------------------------------------------------------------------
def test_page_window_spans_exactly_page_limit_slots():
    """The window [cursor, end] is inclusive on both sides.

    Exactly page_limit slots, not page_limit + 1: with a limit of N the exchange
    returns the N newest records, and an extra slot in the window means
    losing the oldest one.
    """
    from src.collectors.base import page_window_end

    cursor = dt(2026, 1, 1)
    step = timedelta(minutes=15)
    far = dt(2030, 1, 1)

    for limit in (1, 2, 200, 1000):
        end = page_window_end(cursor, step, limit, far)
        slots = int((end - cursor) / step) + 1
        assert slots == limit, f"with limit {limit} the window has {slots} slots"


def test_page_window_respects_hard_end():
    """The requested end of the period matters more than the page size."""
    from src.collectors.base import page_window_end

    cursor = dt(2026, 1, 1)
    hard_end = dt(2026, 1, 1, 3)
    end = page_window_end(cursor, timedelta(hours=1), 1000, hard_end)

    assert end == hard_end


def test_page_window_with_limit_one_does_not_move():
    """A one-record page: the window degenerates to a point rather than going backwards."""
    from src.collectors.base import page_window_end

    cursor = dt(2026, 1, 1)
    assert page_window_end(cursor, timedelta(hours=1), 1, dt(2030, 1, 1)) == cursor


@pytest.mark.parametrize("bad_limit", [0, -1])
def test_page_window_rejects_bad_limit(bad_limit):
    from src.collectors.base import page_window_end

    with pytest.raises(ValueError, match="page size"):
        page_window_end(dt(2026, 1, 1), timedelta(hours=1), bad_limit, dt(2030, 1, 1))


def test_page_window_rejects_non_positive_interval():
    from src.collectors.base import page_window_end

    with pytest.raises(ValueError, match="interval"):
        page_window_end(dt(2026, 1, 1), timedelta(0), 100, dt(2030, 1, 1))


def test_all_collectors_use_the_shared_window_function():
    """Guard against a fourth collector with the same bug.

    Three independently repeated it once; the test catches an attempt to compute
    the window boundary in place instead of calling the shared function.
    """
    import inspect

    from src.collectors import funding, ohlcv, open_interest

    for module in (ohlcv, funding, open_interest):
        source = inspect.getsource(module)
        assert "page_window_end(" in source, f"{module.__name__} does not use the shared function"
        assert "interval * PAGE_LIMIT" not in source, (
            f"{module.__name__} computes the window boundary in place"
        )
