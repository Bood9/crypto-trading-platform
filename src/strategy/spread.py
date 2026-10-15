"""Effective spread estimate from OHLC — a DIAGNOSTIC, not a filter.

DEC-007: the spread takes no part in universe selection and affects nothing.
The estimate is written to a separate column so the distribution across the
universe can be inspected and outliers found.

That is exactly why DEC-001 is not violated here: the setup definition does not
depend on this value, and replacing the estimate with the real spread would not
require a re-backtest.

Method — Abdi & Ranaldo (2017), "A Simple Estimation of Bid-Ask Spreads
from Daily Close, High, and Low Prices". It estimates the effective spread
from the mid of the range and the close price:

    s² = 4 · E[(c_t − η_t) · (c_t − η_{t+1})]

where c is the log close price and η is the log mid (h+l)/2.

A limitation to keep in mind when reading the numbers: the estimate describes
the average order-book width in calm times. It does not measure stop slippage
during a sharp move — that is a different question, and it is covered by the
slippage sweep in the cost model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SpreadEstimate:
    """Spread estimate over a sample of bars."""

    symbol: str
    spread_pct: float | None
    sample_size: int
    #: Share of bars discarded as unusable (zero or invalid prices).
    #: A high value devalues the estimate.
    discarded_share: float = 0.0

    @property
    def reliable(self) -> bool:
        """The estimate can be trusted.

        The sample threshold comes from the method's practice: on short series
        the covariance is so noisy that the estimate is more often negative
        than meaningful.
        """
        return (
            self.spread_pct is not None
            and self.sample_size >= 30
            and self.discarded_share < 0.2
        )


def _mid_log(high: float, low: float) -> float | None:
    if high <= 0 or low <= 0 or high < low:
        return None
    return math.log((high + low) / 2)


def abdi_ranaldo_spread(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    symbol: str = "",
) -> SpreadEstimate:
    """Effective spread estimate in percent.

    Returns None in the `spread_pct` field if the estimate cannot be computed:
    the sample is too small or the covariance came out negative.

    A negative covariance is a normal outcome of the method, not a data error.
    It must not be replaced by zero: zero would read as "no spread", which is
    exactly the opposite of "could not estimate" (P-001).
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows and closes series must be the same length")

    n = len(closes)
    if n < 3:
        return SpreadEstimate(symbol, None, n)

    # Discard bars with unusable prices. Collapses like LUNA produce zeros
    # and extremes on which the logarithm is undefined.
    usable: list[tuple[float, float]] = []  # (log close, log mid)
    discarded = 0
    for h, low, c in zip(highs, lows, closes, strict=True):
        mid = _mid_log(h, low)
        if mid is None or c <= 0 or not math.isfinite(c):
            discarded += 1
            continue
        usable.append((math.log(c), mid))

    discarded_share = discarded / n if n else 0.0
    if len(usable) < 3:
        return SpreadEstimate(symbol, None, len(usable), discarded_share)

    # E[(c_t − η_t) · (c_t − η_{t+1})] over all pairs of adjacent bars.
    terms = [
        (usable[i][0] - usable[i][1]) * (usable[i][0] - usable[i + 1][1])
        for i in range(len(usable) - 1)
    ]
    if not terms:
        return SpreadEstimate(symbol, None, len(usable), discarded_share)

    covariance = sum(terms) / len(terms)
    squared = 4 * covariance
    if squared <= 0:
        # The method failed: noise drowned the signal. This is not "spread is zero".
        return SpreadEstimate(symbol, None, len(usable), discarded_share)

    return SpreadEstimate(
        symbol=symbol,
        spread_pct=math.sqrt(squared) * 100,
        sample_size=len(usable),
        discarded_share=discarded_share,
    )


def describe_distribution(estimates: list[SpreadEstimate]) -> dict[str, float | int]:
    """Universe summary: median, quartiles, share of non-computable estimates.

    The point of the diagnostic is not a threshold but the shape of the
    distribution and the outliers. A symbol with a spread several times the
    median is worth a look by eye, even if its turnover is decent.
    """
    values = sorted(e.spread_pct for e in estimates if e.reliable and e.spread_pct)
    total = len(estimates)
    if not values:
        return {"count": 0, "unreliable": total}

    def quantile(q: float) -> float:
        if len(values) == 1:
            return values[0]
        position = q * (len(values) - 1)
        low = int(position)
        high = min(low + 1, len(values) - 1)
        weight = position - low
        return values[low] * (1 - weight) + values[high] * weight

    return {
        "count": len(values),
        "unreliable": total - len(values),
        "min": values[0],
        "p25": quantile(0.25),
        "median": quantile(0.5),
        "p75": quantile(0.75),
        "p95": quantile(0.95),
        "max": values[-1],
    }
