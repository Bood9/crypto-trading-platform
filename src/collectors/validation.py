"""Validation of collected data.

A matching row count does not mean integrity. The checks target what
breaks calculations further down the chain:

* **timestamp monotonicity** — bars arriving out of order break any
  window indicator;
* **duplicates by key** — with upsert there should be none, but the check
  is cheap and catches a bug in the key itself;
* **zero volume in the middle of an active period** — a sign that the
  exchange returned a placeholder bar rather than a real candle;
* **gaps longer than one interval** — on thin pairs Bybit skips bars when
  there are no trades, and such a gap exists as a MISSING row, not as a
  row with zero.

The last one matters most. ATR(14) over fourteen bars with holes is not
ATR(14): the window silently stretches over a longer period and
volatility is understated. The scale of the phenomenon decides whether
indicators must be computed on a calendar grid with explicit gap handling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from src.core.db import Ohlcv, session_scope
from src.core.logging import get_logger

log = get_logger(__name__)

TIMEFRAME_INTERVAL: dict[str, timedelta] = {
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


@dataclass
class GapRun:
    """One gap: how many bars are missing and when."""

    start: datetime
    end: datetime
    missing: int


#: Coefficient of variation threshold below which gaps are considered
#: suspiciously even. Chosen from the facts: for the bug found, the
#: intervals between gaps matched to the bar (CV = 0), while market gaps
#: have a spread comparable to the mean (CV around 1).
#: 0.25 leaves a margin for a partially regular pattern.
REGULARITY_CV_THRESHOLD = 0.25

#: Fewer than three gaps do not give two intervals between them —
#: there is nothing to judge regularity by.
MIN_GAPS_FOR_REGULARITY = 4


@dataclass
class RegularityCheck:
    """P-004: regularity where randomness is expected.

    Market gaps come from a lack of trades and are distributed unevenly.
    If the intervals between holes are almost identical, the cause is not
    the market but the collection — for example, page boundary arithmetic.

    This is how the bug losing one bar per thousand was found: the only
    visible trace was the median and the 95th percentile of gaps matching.
    """

    gaps: int = 0
    mean_interval: float = 0.0
    stdev_interval: float = 0.0
    #: Coefficient of variation: standard deviation over the mean.
    #: Dimensionless, so comparable across timeframes.
    cv: float | None = None
    #: Most likely period in bars, if regularity is detected.
    period_bars: int | None = None

    @property
    def suspicious(self) -> bool:
        return self.cv is not None and self.cv < REGULARITY_CV_THRESHOLD

    def describe(self) -> str:
        if self.cv is None:
            return "too few gaps to judge"
        if not self.suspicious:
            return f"gaps are irregular (CV={self.cv:.2f}) — looks like the market"
        return (
            f"SUSPICIOUSLY EVEN gaps: CV={self.cv:.2f}, "
            f"period ≈ {self.period_bars} bars. "
            "Regularity where randomness is expected is a sign "
            "of a collection defect, not a market property (P-004)."
        )


@dataclass
class ValidationReport:
    symbol: str
    timeframe: str
    rows: int = 0
    first_ts: datetime | None = None
    last_ts: datetime | None = None

    duplicates: int = 0
    out_of_order: int = 0
    zero_volume_bars: int = 0
    gaps: list[GapRun] = field(default_factory=list)
    expected_bars: int = 0

    @property
    def missing_bars(self) -> int:
        return sum(g.missing for g in self.gaps)

    @property
    def gap_share(self) -> float:
        """Share of missing bars out of the expected count."""
        if self.expected_bars <= 0:
            return 0.0
        return self.missing_bars / self.expected_bars

    @property
    def longest_gap(self) -> int:
        return max((g.missing for g in self.gaps), default=0)

    @property
    def clean(self) -> bool:
        return (
            self.duplicates == 0
            and self.out_of_order == 0
            and self.zero_volume_bars == 0
            and not self.gaps
        )

    def regularity(self, interval: timedelta) -> RegularityCheck:
        """How evenly the gaps are spaced.

        Computed from the intervals BETWEEN holes, not from their length:
        a collection bug produces gaps every constant number of bars, while
        the length of each is the same and adds nothing.
        """
        check = RegularityCheck(gaps=len(self.gaps))
        if len(self.gaps) < MIN_GAPS_FOR_REGULARITY:
            return check

        starts = sorted(g.start for g in self.gaps)
        step_seconds = interval.total_seconds()
        if step_seconds <= 0:
            return check

        intervals = [
            (b - a).total_seconds() / step_seconds
            for a, b in zip(starts, starts[1:], strict=False)
        ]
        if len(intervals) < 2:
            return check

        mean = sum(intervals) / len(intervals)
        if mean <= 0:
            return check
        variance = sum((x - mean) ** 2 for x in intervals) / len(intervals)
        stdev = variance**0.5

        check.mean_interval = mean
        check.stdev_interval = stdev
        check.cv = stdev / mean
        check.period_bars = round(mean)
        return check

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.duplicates:
            out.append(f"duplicates {self.duplicates}")
        if self.out_of_order:
            out.append(f"out-of-order {self.out_of_order}")
        if self.zero_volume_bars:
            out.append(f"zero-volume bars {self.zero_volume_bars}")
        if self.gaps:
            out.append(
                f"gaps {len(self.gaps)} "
                f"(missing {self.missing_bars}, max {self.longest_gap})"
            )
        return out


async def validate_ohlcv(
    symbol: str,
    timeframe: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> ValidationReport:
    """Checks one candle series.

    Timestamps are fetched in full: for 15m over two years that is 70
    thousand values — acceptable, whereas counting gaps in SQL would need
    a window function that is harder to read and verify.
    """
    interval = TIMEFRAME_INTERVAL.get(timeframe)
    if interval is None:
        # P-001: an unknown timeframe is not replaced by the nearest one.
        raise ValueError(f"unknown timeframe {timeframe!r}")

    report = ValidationReport(symbol=symbol, timeframe=timeframe)

    query = select(Ohlcv.ts, Ohlcv.volume, Ohlcv.turnover).where(
        Ohlcv.symbol == symbol, Ohlcv.timeframe == timeframe
    )
    if start is not None:
        query = query.where(Ohlcv.ts >= start)
    if end is not None:
        query = query.where(Ohlcv.ts < end)
    query = query.order_by(Ohlcv.ts)

    async with session_scope() as session:
        rows = (await session.execute(query)).all()

    if not rows:
        return report

    report.rows = len(rows)
    report.first_ts = rows[0][0]
    report.last_ts = rows[-1][0]
    report.expected_bars = (
        int((report.last_ts - report.first_ts) / interval) + 1 if interval else 0
    )

    previous: datetime | None = None
    for ts, volume, turnover in rows:
        if previous is not None:
            if ts == previous:
                report.duplicates += 1
            elif ts < previous:
                report.out_of_order += 1
            else:
                step = ts - previous
                if step > interval:
                    missing = int(step / interval) - 1
                    report.gaps.append(GapRun(previous, ts, missing))

        # Zero volume with non-zero turnover is impossible — so this is
        # exactly a placeholder bar, not rounding.
        if volume == 0 and turnover == 0:
            report.zero_volume_bars += 1

        previous = ts

    return report


async def duplicate_keys(timeframe: str) -> int:
    """Duplicates by primary key across the whole table.

    With upsert there can be none, but the check catches a bug in the key
    itself — for example, if timeframe had dropped out of the PK.
    """
    query = (
        select(func.count())
        .select_from(
            select(Ohlcv.symbol, Ohlcv.ts, func.count().label("n"))
            .where(Ohlcv.timeframe == timeframe)
            .group_by(Ohlcv.symbol, Ohlcv.ts)
            .having(func.count() > 1)
            .subquery()
        )
    )
    async with session_scope() as session:
        return (await session.execute(query)).scalar_one()


def percentile(values: list[float], q: float) -> float:
    """Percentile by linear interpolation.

    Our own, not numpy's: numpy is not needed in the collection
    dependencies, and the formula here is three lines.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


@dataclass
class GapDistribution:
    """Summary of gaps across the whole universe.

    These numbers decide how indicators are computed. ATR(14) over a
    window with holes is not ATR(14): the window silently stretches over
    a longer period and volatility is understated. If gaps are widespread,
    computation will have to use a calendar grid with explicit handling
    of missing bars.
    """

    symbols: int = 0
    symbols_with_gaps: int = 0
    total_missing: int = 0
    total_expected: int = 0
    #: Missing bars per symbol per month — the basis for median and percentiles.
    per_symbol_month: list[float] = field(default_factory=list)
    longest_gap_overall: int = 0
    worst: list[tuple[str, float, int]] = field(default_factory=list)
    #: Symbols with suspiciously even gaps (P-004).
    suspicious_regularity: list[tuple[str, float, int]] = field(default_factory=list)

    @property
    def share_with_gaps(self) -> float:
        return self.symbols_with_gaps / self.symbols if self.symbols else 0.0

    @property
    def overall_gap_share(self) -> float:
        return self.total_missing / self.total_expected if self.total_expected else 0.0

    @property
    def median_per_symbol_month(self) -> float:
        return percentile(self.per_symbol_month, 0.5)

    @property
    def p95_per_symbol_month(self) -> float:
        return percentile(self.per_symbol_month, 0.95)


def summarize_bar_gaps(
    reports: list[ValidationReport], *, top: int = 15
) -> GapDistribution:
    dist = GapDistribution(symbols=len(reports))
    interval = TIMEFRAME_INTERVAL.get(reports[0].timeframe) if reports else None
    for r in reports:
        dist.total_missing += r.missing_bars
        dist.total_expected += r.expected_bars
        dist.longest_gap_overall = max(dist.longest_gap_overall, r.longest_gap)
        if r.rows and r.first_ts and r.last_ts:
            # Normalise by the actual series length, not the overall period:
            # otherwise a symbol with half a year of history would look three
            # times cleaner than it is.
            span_days = (r.last_ts - r.first_ts).total_seconds() / 86400
            span_months = max(span_days / 30.44, 1 / 30.44)
            dist.per_symbol_month.append(r.missing_bars / span_months)
        if r.gaps:
            dist.symbols_with_gaps += 1
            if interval is not None:
                check = r.regularity(interval)
                if check.suspicious:
                    dist.suspicious_regularity.append(
                        (r.symbol, check.cv or 0.0, check.period_bars or 0)
                    )
    dist.worst = sorted(
        ((r.symbol, r.gap_share, r.longest_gap) for r in reports if r.gaps),
        key=lambda x: -x[1],
    )[:top]
    return dist


__all__ = [
    "GapDistribution",
    "GapRun",
    "TIMEFRAME_INTERVAL",
    "ValidationReport",
    "duplicate_keys",
    "summarize_bar_gaps",
    "validate_ohlcv",
]
