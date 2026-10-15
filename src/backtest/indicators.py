"""Indicators. Pure functions over bar series.

Computed over the AVAILABLE bars, without filling in the calendar grid.
The basis is a measurement on the collected data: gaps exist
(88% of symbols), but are always single, the maximum continuous gap
is one bar, and the total share of gaps is 0.07%. At such a
density the ATR(14) window stretches to 15 periods in fewer than
one case in a thousand.

If the picture on 15m turns out worse, the decision will have to be revisited,
but it will have to be revisited here, in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Bar:
    high: Decimal
    low: Decimal
    close: Decimal


def true_range(bar: Bar, previous_close: Decimal | None) -> Decimal:
    """True range of one bar.

    Without a previous close it reduces to the bar's range: there is nothing
    to measure a gap from.
    """
    span = bar.high - bar.low
    if previous_close is None:
        return max(span, Decimal(0))
    return max(
        span,
        abs(bar.high - previous_close),
        abs(bar.low - previous_close),
    )


def atr(bars: list[Bar], period: int = 14) -> Decimal | None:
    """Average True Range by Wilder's method.

    Returns None if there are fewer bars than the period or the result
    cannot be computed. Not zero: zero would mean "no volatility",
    which for a stop calculation is the opposite of "could not compute"
    (P-001). A stop on a zero ATR would coincide with the entry price.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")
    if len(bars) < period:
        return None

    try:
        ranges: list[Decimal] = []
        previous_close: Decimal | None = None
        for bar in bars:
            if not _is_finite(bar.high) or not _is_finite(bar.low) or not _is_finite(bar.close):
                # The LUNA collapse produces zeros and degenerate values.
                # There is nothing to fill them with, and silently skipping is not allowed.
                return None
            ranges.append(true_range(bar, previous_close))
            previous_close = bar.close

        # Wilder smoothing: the first value is a simple average,
        # then recursive.
        value = sum(ranges[:period]) / Decimal(period)
        for tr in ranges[period:]:
            value = (value * (period - 1) + tr) / Decimal(period)

        if not _is_finite(value) or value < 0:
            return None
        return value
    except (InvalidOperation, ZeroDivisionError, OverflowError):
        return None


def atr_pct(bars: list[Bar], period: int = 14) -> Decimal | None:
    """ATR as a fraction of the last close price.

    Needed because the stop is set in percent of the move, not in absolute terms:
    otherwise comparing ATR between symbols priced $0.000002 and $65 000
    is meaningless.
    """
    value = atr(bars, period)
    if value is None:
        return None
    last_close = bars[-1].close
    if last_close <= 0:
        return None
    result = value / last_close
    return result if _is_finite(result) else None


def _is_finite(value: Decimal) -> bool:
    """Decimal supports NaN and Infinity: they come from dividing
    degenerate values and silently poison the further calculation."""
    return value.is_finite()
