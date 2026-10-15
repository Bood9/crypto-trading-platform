"""Series validation tests.

A matching row count does not mean integrity: these checks catch exactly
what breaks the calculations downstream. ATR over a window with holes
understates volatility, and a zero-volume placeholder bar looks like a real
candle with no movement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.collectors.validation import (
    GapRun,
    ValidationReport,
    summarize_bar_gaps,
)

HOUR = timedelta(hours=1)
T0 = datetime(2026, 8, 1, tzinfo=UTC)


def report(symbol="SOLUSDT", **kwargs) -> ValidationReport:
    base = {"symbol": symbol, "timeframe": "1h"}
    base.update(kwargs)
    return ValidationReport(**base)


# ---------------------------------------------------------------------------
# Properties of a single report
# ---------------------------------------------------------------------------
def test_clean_report_has_no_problems():
    r = report(rows=100, expected_bars=100)
    assert r.clean
    assert r.problems() == []


def test_gaps_are_counted_and_summed():
    r = report(
        rows=90,
        expected_bars=100,
        gaps=[GapRun(T0, T0 + 3 * HOUR, 2), GapRun(T0 + 10 * HOUR, T0 + 19 * HOUR, 8)],
    )
    assert r.missing_bars == 10
    assert r.longest_gap == 8
    assert r.gap_share == pytest.approx(0.1)
    assert not r.clean


def test_duplicates_and_order_break_cleanliness():
    assert not report(rows=10, duplicates=1).clean
    assert not report(rows=10, out_of_order=1).clean
    assert not report(rows=10, zero_volume_bars=1).clean


def test_gap_share_on_empty_series():
    """An empty series must not cause division by zero."""
    assert report().gap_share == 0.0
    assert report().longest_gap == 0


def test_problems_are_human_readable():
    r = report(rows=5, duplicates=2, gaps=[GapRun(T0, T0 + 2 * HOUR, 1)])
    text = " ".join(r.problems())
    assert "duplicates 2" in text
    assert "gaps 1" in text


# ---------------------------------------------------------------------------
# Distribution across the universe
# ---------------------------------------------------------------------------
def test_distribution_counts_symbols_with_gaps():
    reports = [
        report("A", rows=100, expected_bars=100),
        report("B", rows=95, expected_bars=100, gaps=[GapRun(T0, T0 + 6 * HOUR, 5)]),
        report("C", rows=90, expected_bars=100, gaps=[GapRun(T0, T0 + 11 * HOUR, 10)]),
    ]
    dist = summarize_bar_gaps(reports)

    assert dist.symbols == 3
    assert dist.symbols_with_gaps == 2
    assert dist.total_missing == 15
    assert dist.share_with_gaps == pytest.approx(2 / 3)
    assert dist.overall_gap_share == pytest.approx(15 / 300)


def test_worst_is_sorted_by_share_not_absolute_count():
    """A symbol with a short series and a couple of holes is worse than a long
    one with the same number of holes."""
    reports = [
        report("LONG", rows=9_900, expected_bars=10_000, gaps=[GapRun(T0, T0, 100)]),
        report("SHORT", rows=90, expected_bars=100, gaps=[GapRun(T0, T0, 10)]),
    ]
    dist = summarize_bar_gaps(reports)
    assert dist.worst[0][0] == "SHORT"


def test_distribution_on_empty_input():
    dist = summarize_bar_gaps([])
    assert dist.symbols == 0
    assert dist.share_with_gaps == 0.0
    assert dist.overall_gap_share == 0.0


def test_clean_symbols_are_absent_from_worst():
    reports = [report("A", rows=100, expected_bars=100)]
    assert summarize_bar_gaps(reports).worst == []


# ---------------------------------------------------------------------------
# Series parsing
# ---------------------------------------------------------------------------
def test_unknown_timeframe_is_rejected():
    """P-001: an unknown timeframe is not replaced by the nearest one."""
    from src.collectors.validation import TIMEFRAME_INTERVAL

    assert "30m" not in TIMEFRAME_INTERVAL
    assert set(TIMEFRAME_INTERVAL) == {"15m", "1h", "4h"}


# ---------------------------------------------------------------------------
# Distribution metrics
# ---------------------------------------------------------------------------
def test_percentile_interpolates():
    from src.collectors.validation import percentile

    values = [float(i) for i in range(1, 101)]
    assert percentile(values, 0.5) == pytest.approx(50.5)
    assert percentile(values, 0.95) == pytest.approx(95.05)
    assert percentile(values, 0.0) == 1.0
    assert percentile(values, 1.0) == 100.0


def test_percentile_on_edge_cases():
    from src.collectors.validation import percentile

    assert percentile([], 0.5) == 0.0
    assert percentile([7.0], 0.95) == 7.0


def test_per_symbol_month_is_normalised_by_actual_span():
    """A symbol with a short series must not look cleaner than it is.

    Two symbols with the same number of missing bars but different series
    lengths have different gap densities, and density is what matters for
    computing indicators.
    """
    long_series = report(
        "LONG",
        rows=1000,
        expected_bars=1000,
        first_ts=datetime(2024, 8, 1, tzinfo=UTC),
        last_ts=datetime(2026, 8, 1, tzinfo=UTC),
        gaps=[GapRun(T0, T0, 24)],
    )
    short_series = report(
        "SHORT",
        rows=100,
        expected_bars=100,
        first_ts=datetime(2026, 6, 1, tzinfo=UTC),
        last_ts=datetime(2026, 8, 1, tzinfo=UTC),
        gaps=[GapRun(T0, T0, 24)],
    )
    dist = summarize_bar_gaps([long_series, short_series])

    # 24 missing bars over 24 months versus 24 over 2 months.
    assert dist.p95_per_symbol_month > dist.median_per_symbol_month


def test_longest_gap_is_tracked_across_universe():
    reports = [
        report("A", rows=10, expected_bars=10, gaps=[GapRun(T0, T0, 3)]),
        report("B", rows=10, expected_bars=10, gaps=[GapRun(T0, T0, 99)]),
    ]
    assert summarize_bar_gaps(reports).longest_gap_overall == 99


def test_symbols_without_timestamps_are_skipped_in_density():
    """An empty series must not cause division by zero."""
    dist = summarize_bar_gaps([report("EMPTY")])
    assert dist.per_symbol_month == []
    assert dist.median_per_symbol_month == 0.0


# ---------------------------------------------------------------------------
# P-004: gap regularity
# ---------------------------------------------------------------------------
def test_regular_gaps_are_flagged_as_collector_defect():
    """The signature of the bug found: a gap exactly every 1001 bars.

    Random market gaps never show such a pattern.
    """
    step = timedelta(minutes=15)
    gaps = [GapRun(T0 + step * 1001 * i, T0 + step * 1001 * i + step * 2, 1) for i in range(8)]
    r = report(rows=8000, expected_bars=8008, gaps=gaps, timeframe="15m")

    check = r.regularity(step)

    assert check.suspicious
    assert check.cv == pytest.approx(0.0, abs=1e-9)
    assert check.period_bars == 1001
    assert "SUSPICIOUSLY" in check.describe()


def test_random_gaps_are_not_flagged():
    """Market gaps are irregular, so there is nothing to warn about."""
    import random

    rng = random.Random(11)
    step = timedelta(hours=1)
    position = 0
    gaps = []
    for _ in range(12):
        position += rng.randint(5, 900)
        gaps.append(GapRun(T0 + step * position, T0 + step * (position + 2), 1))

    check = report(rows=5000, expected_bars=5012, gaps=gaps).regularity(step)

    assert not check.suspicious
    assert check.cv is not None and check.cv > 0.25


def test_too_few_gaps_yields_no_verdict():
    """P-001: too little data is no reason to invent a conclusion."""
    step = timedelta(hours=1)
    gaps = [GapRun(T0, T0 + step * 2, 1), GapRun(T0 + step * 50, T0 + step * 52, 1)]

    check = report(rows=100, expected_bars=102, gaps=gaps).regularity(step)

    assert check.cv is None
    assert not check.suspicious
    assert "too few" in check.describe()


def test_zero_interval_does_not_divide_by_zero():
    check = report(rows=10, expected_bars=10).regularity(timedelta(0))
    assert check.cv is None


def test_partially_regular_gaps_are_still_flagged():
    """The bug could produce a not perfectly even pattern, for example if
    some pages were cut short. The threshold leaves a margin for that."""
    step = timedelta(minutes=15)
    offsets = [0, 1000, 2001, 3000, 4002, 5001, 6000, 7001]
    gaps = [GapRun(T0 + step * o, T0 + step * (o + 2), 1) for o in offsets]

    check = report(rows=8000, expected_bars=8008, gaps=gaps).regularity(step)

    assert check.suspicious
    assert check.period_bars == pytest.approx(1000, abs=2)


def test_distribution_collects_suspicious_symbols():
    step = timedelta(minutes=15)
    regular = [GapRun(T0 + step * 1001 * i, T0 + step * 1001 * i + step * 2, 1) for i in range(6)]
    reports = [
        report("BUGGY", rows=6000, expected_bars=6006, gaps=regular, timeframe="15m"),
        report("CLEAN", rows=6000, expected_bars=6000, timeframe="15m"),
    ]
    dist = summarize_bar_gaps(reports)

    assert len(dist.suspicious_regularity) == 1
    assert dist.suspicious_regularity[0][0] == "BUGGY"
