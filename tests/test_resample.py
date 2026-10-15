"""Daily bars from 4-hour ones. DEC-033."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import MarketBar
from src.backtest.resample import to_daily

D = Decimal
H4 = timedelta(hours=4)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def bar(ts, o, h, low, c, v="1") -> MarketBar:
    return MarketBar(ts, D(str(o)), D(str(h)), D(str(low)), D(str(c)), D(str(v)))


def full_day(day: datetime, base: float = 100.0) -> list[MarketBar]:
    return [
        bar(day + H4 * i, base + i, base + i + 5, base + i - 5, base + i + 1, v=str(10 + i))
        for i in range(6)
    ]


def test_ohlcv_is_aggregated_correctly():
    (d,) = to_daily(full_day(T0), H4)
    assert d.ts == T0
    assert d.open == D("100")          # first bar
    assert d.close == D("106")         # last bar
    assert d.high == D("110")          # high
    assert d.low == D("95")            # low
    assert d.volume == D(sum(10 + i for i in range(6)))


def test_incomplete_day_is_dropped_not_filled():
    """P-001: an incomplete day would give a wrong price."""
    day = full_day(T0)
    del day[3]
    assert to_daily(day, H4) == []


def test_current_unfinished_day_never_appears():
    """Otherwise the last "daily" bar would contain the future."""
    bars = full_day(T0) + full_day(T0 + timedelta(days=1))[:4]
    daily = to_daily(bars, H4)
    assert [d.ts for d in daily] == [T0]


def test_days_are_utc_and_ordered():
    bars = full_day(T0 + timedelta(days=1)) + full_day(T0)
    assert [d.ts for d in to_daily(bars, H4)] == [T0, T0 + timedelta(days=1)]


def test_misaligned_bars_do_not_make_a_day():
    shifted = [
        bar(T0 + timedelta(hours=1) + H4 * i, 100, 101, 99, 100) for i in range(6)
    ]
    assert to_daily(shifted, H4) == []


def test_interval_must_divide_a_day():
    with pytest.raises(ValueError):
        to_daily([], timedelta(hours=5))
