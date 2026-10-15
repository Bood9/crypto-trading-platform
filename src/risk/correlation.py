"""Return correlation. Spec §5, P-003.

Needed in two places and for one reason. At the position level the governor
rejects a second bet on the same move; at the logic level P-003 keeps out a
setup that repeats one already accepted. In both cases the question is the same:
are the two bets really independent.

Computed on LOG returns over a common time grid. Common — because two symbols
have different gaps, and correlating by bar index would silently match different
moments in time: a series with a hole would shift relative to its neighbour and
show a relationship where there is none.

What cannot be computed stays None and is NOT replaced by zero (P-001). Zero
here would mean "no relationship" — i.e. permission to open a second position —
where in fact there is nothing to judge by.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import select

from src.core.db import Ohlcv, session_scope

#: Minimum number of common observations. A correlation over a couple of dozen
#: points jumps around so much that its sign is unreliable, and the decision is
#: made against a 0.7 threshold — a magnitude such a sample cannot
#: distinguish.
MIN_OVERLAP = 120

#: Minimum share of the window the common observations must cover. A threshold
#: on top of the absolute one: 120 points out of 720 is no longer "the same
#: thirty days" but two short pieces that happened to coincide in time.
MIN_OVERLAP_SHARE = 0.5

#: Timeframe of the correlation window. Hourly: 4h gives 180 points a month,
#: 15m gives 2880, and the latter takes noticeably longer for precision the
#: 0.7 threshold does not need anyway.
TIMEFRAME = "1h"


def modal_interval(series: list[tuple[datetime, Decimal]]) -> timedelta | None:
    """The most frequent step of a series. Needed to tell adjacent bars
    from bars on the edges of a gap."""
    gaps: dict[timedelta, int] = {}
    for (earlier, _), (later, _) in zip(series, series[1:], strict=False):
        step = later - earlier
        if step > timedelta(0):
            gaps[step] = gaps.get(step, 0) + 1
    if not gaps:
        return None
    return max(gaps.items(), key=lambda kv: kv[1])[0]


def log_returns(
    series: list[tuple[datetime, Decimal]],
    interval: timedelta | None = None,
) -> dict[datetime, float]:
    """Returns keyed by the CLOSE time of the interval.

    The key is the moment of the pair's second bar: the return belongs to the
    transition from the previous bar to the current one, and dating it by the
    start of the period would shift the whole series one bar back.

    **No return is computed across a gap.** Bars adjacent in the list on the
    edges of a hole are several intervals apart, and the move between them is a
    four-bar move passed off as a one-bar move. Measured: one such outlier in
    three hundred points drops the correlation of two IDENTICAL series from 1.00
    to 0.79, i.e. turns a certain rejection into permission to open a second
    position.

    The series step is inferred as the most frequent one unless given
    explicitly: each symbol has its own, and requiring it as an argument
    everywhere would mean dragging the timeframe through all the calling code.
    """
    step = interval or modal_interval(series)
    out: dict[datetime, float] = {}
    previous: tuple[datetime, Decimal] | None = None
    for ts, close in series:
        if close is None or close <= 0 or not close.is_finite():
            previous = None
            continue
        if previous is not None and (step is None or ts - previous[0] == step):
            with contextlib.suppress(
                InvalidOperation, ZeroDivisionError, OverflowError
            ):
                out[ts] = float(close / previous[1]) - 1.0
        previous = (ts, close)
    return out


def pearson(a: list[float], b: list[float]) -> float | None:
    """Correlation coefficient. None for a degenerate sample.

    Degeneracy is not "correlation equals zero": a constant-price series
    (a delisted pair, halted trading) is not independent of its neighbour,
    it simply carries no information.
    """
    n = len(a)
    if n != len(b) or n < 2:
        return None
    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a <= 0 or var_b <= 0:
        return None
    denominator = (var_a * var_b) ** 0.5
    if denominator <= 0:
        return None
    value = cov / denominator
    # Accumulated floating-point error can push the result outside
    # [-1, 1] by fractions of an epsilon.
    return max(-1.0, min(1.0, value))


def correlate(
    first: dict[datetime, float],
    second: dict[datetime, float],
    *,
    expected: int,
    min_overlap: int = MIN_OVERLAP,
    min_share: float = MIN_OVERLAP_SHARE,
) -> Decimal | None:
    """Correlation of two return series over COMMON moments in time."""
    common = sorted(first.keys() & second.keys())
    if len(common) < min_overlap:
        return None
    if expected > 0 and len(common) / expected < min_share:
        return None
    value = pearson([first[ts] for ts in common], [second[ts] for ts in common])
    return None if value is None else Decimal(str(round(value, 6)))


class CorrelationSource:
    """Correlations from stored candles, memoised for the duration of a run.

    The cache is mandatory, not optional: the governor asks for a correlation
    on every signal, and the thirty-day series are the same each time. Without
    it the limit check turns into a database query for every candle.

    The cache key includes the moment, rounded down to the day: the window
    slides, and yesterday's correlation is a different value today.
    """

    #: Candle step per timeframe. Set explicitly rather than inferred from the
    #: series: for a gappy symbol the most frequent step could be a gap.
    INTERVALS = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "4h": timedelta(hours=4),
    }

    def __init__(self, lookback_days: int, timeframe: str = TIMEFRAME) -> None:
        if timeframe not in self.INTERVALS:
            raise ValueError(f"unknown timeframe {timeframe!r}")
        self.lookback_days = lookback_days
        self.timeframe = timeframe
        self.interval = self.INTERVALS[timeframe]
        self._returns: dict[tuple[str, object], dict[datetime, float]] = {}
        self._pairs: dict[tuple[str, str, object], Decimal | None] = {}

    @property
    def expected_points(self) -> int:
        """How many observations a full window without gaps gives."""
        per_day = {"15m": 96, "1h": 24, "4h": 6}.get(self.timeframe, 24)
        return self.lookback_days * per_day

    async def returns(self, symbol: str, moment: datetime) -> dict[datetime, float]:
        key = (symbol, moment.date())
        cached = self._returns.get(key)
        if cached is not None:
            return cached

        start = moment - timedelta(days=self.lookback_days)
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(Ohlcv.ts, Ohlcv.close)
                    .where(
                        Ohlcv.symbol == symbol,
                        Ohlcv.timeframe == self.timeframe,
                        Ohlcv.ts >= start,
                        Ohlcv.ts <= moment,
                    )
                    .order_by(Ohlcv.ts)
                )
            ).all()
        result = log_returns(
            [(ts, close) for ts, close in rows], self.interval
        )
        self._returns[key] = result
        return result

    async def between(
        self, left: str, right: str, moment: datetime
    ) -> Decimal | None:
        """Correlation of a pair. Symbol order does not matter."""
        if left == right:
            return Decimal(1)
        first, second = sorted((left, right))
        key = (first, second, moment.date())
        if key in self._pairs:
            return self._pairs[key]

        value = correlate(
            await self.returns(first, moment),
            await self.returns(second, moment),
            expected=self.expected_points,
        )
        self._pairs[key] = value
        return value


def series_correlation(
    left: list[tuple[datetime, Decimal]],
    right: list[tuple[datetime, Decimal]],
    *,
    expected: int = 0,
    min_overlap: int = MIN_OVERLAP,
    interval: timedelta | None = None,
) -> Decimal | None:
    """Correlation of two price series directly. For tests and for P-003,
    where the series come not from candles but from setup results."""
    return correlate(
        log_returns(left, interval),
        log_returns(right, interval),
        expected=expected,
        min_overlap=min_overlap,
        min_share=0.0,
    )


__all__ = [
    "MIN_OVERLAP",
    "MIN_OVERLAP_SHARE",
    "TIMEFRAME",
    "CorrelationSource",
    "correlate",
    "log_returns",
    "modal_interval",
    "pearson",
    "series_correlation",
]
