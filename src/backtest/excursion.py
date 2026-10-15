"""Maximum favourable excursion after entry (MFE), in R units.

Answers the question that must be asked BEFORE choosing a target: what move
the market gives at all over the holding horizon. A target assigned before this
measurement tests not the setup's hypothesis but the compatibility of two numbers
taken independently of each other: exactly what both Setup B (1.1%
hit rate) and the first run of Setup A (4.2%) burned on, see P-005.

The measurement is diagnostic: it looks forward from the entry and therefore HAS NO
RIGHT to take part in decisions. It runs over an already finished list of
trades, in a separate pass, and returns nothing to the signal function.
The boundary here is the same as the engine's: what the strategy sees and what
the researcher sees are different things, and they must not be mixed.

TWO quantities are computed, and the difference between them is a result in itself:

* **attainable**: the maximum in our favour before we are taken out by
  the stop or the time stop. THE CURRENT TARGET IS IGNORED here deliberately:
  a measurement cut off by the active target could not discover that
  a larger one is attainable. This is the quantity that determines target
  attainability: target T is hit in exactly those trades where the move reached T,
  and the exit at T does not affect that;
* **horizon**: the maximum in our favour up to the time stop, the stop is also
  ignored. The physics of the move without our construction.

If the first is much smaller than the second, the target is unattainable not by the
nature of the move but because the stop takes us out earlier. Both can be
negative: that means the price did not move in our favour at all,
and such trades must not be replaced with zero: the tail of the distribution is
the very subject of the measurement.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from src.backtest.accounting import TradeOutcome
from src.backtest.engine import MarketBar


@dataclass(frozen=True)
class Excursion:
    """Price excursion after one trade.

    What cannot be computed stays None and is not replaced with zero (P-001): a
    trade whose horizon ran into the end of the data is not a trade
    with a zero move.
    """

    symbol: str
    opened_at: datetime
    is_long: bool
    risk: Decimal

    #: Maximum in our favour before being taken out by the stop or time stop.
    #: The active target is not considered; see the module header.
    attainable_r: Decimal | None
    #: Maximum in our favour up to the time stop, the stop is ignored.
    horizon_r: Decimal | None
    #: Maximum AGAINST us up to the time stop, the stop is ignored.
    adverse_r: Decimal | None

    bars_held: int
    #: On which bar after entry attainable_r was reached.
    bars_to_peak: int | None
    close_reason: str | None
    #: The horizon did not fit into the available data: the trade is not counted.
    truncated: bool = False
    invalid_reason: str | None = None

    @property
    def valid(self) -> bool:
        return (
            self.attainable_r is not None
            and self.horizon_r is not None
            and not self.truncated
            and self.invalid_reason is None
        )


def measure(
    bars: list[MarketBar],
    outcomes: list[TradeOutcome],
    time_stop: timedelta,
    *,
    symbol: str = "",
) -> list[Excursion]:
    """Price excursion for each trade of one symbol.

    The entry is found by the open time: the engine opens a position at the bar
    close, so counting starts from the NEXT bar: an entry and exit within
    one bar would mean knowing its future.

    The horizon ends with the bar on which the time stop fires,
    INCLUSIVE: the engine checks the target on that bar before the
    time stop, so the target can still be hit on it.

    Prices are taken at the signal's LEVELS, without slippage and fees.
    The market's move is measured, not our result: costs shift any
    target equally and have nothing to do with its attainability.
    """
    index = {bar.ts: i for i, bar in enumerate(bars)}
    out: list[Excursion] = []

    for outcome in outcomes:
        trade = outcome.trade
        start = index.get(trade.opened_at)
        if start is None:
            out.append(
                _invalid(trade, symbol, "entry bar not found in the series")
            )
            continue

        risk = trade.risk_per_unit
        if risk <= 0 or not risk.is_finite():
            out.append(_invalid(trade, symbol, "zero distance to stop"))
            continue

        long = trade.is_long
        entry = trade.entry
        stop = trade.stop

        attainable: Decimal | None = None
        horizon: Decimal | None = None
        adverse: Decimal | None = None
        bars_to_peak: int | None = None
        bars_held = 0
        stopped = False
        reached_horizon = False

        try:
            for offset in range(1, len(bars) - start):
                bar = bars[start + offset]
                bars_held = offset
                last = bar.ts - trade.opened_at >= time_stop

                favourable = (bar.high - entry) if long else (entry - bar.low)
                against = (entry - bar.low) if long else (bar.high - entry)

                # Horizon: there is no stop, every bar is taken.
                h = favourable / risk
                if horizon is None or h > horizon:
                    horizon = h
                a = against / risk
                if adverse is None or a > adverse:
                    adverse = a

                # Actual trade: on the bar that touched the stop, the favourable
                # extreme is not counted. The order of events within a bar
                # is unknown, and assuming the maximum came first is
                # exactly the inflation that `_check_exit` guards against.
                if not stopped:
                    hit = bar.low <= stop if long else bar.high >= stop
                    if hit:
                        stopped = True
                    else:
                        r = favourable / risk
                        if attainable is None or r > attainable:
                            attainable = r
                            bars_to_peak = offset

                if last:
                    reached_horizon = True
                    break
        except (InvalidOperation, ZeroDivisionError, OverflowError) as exc:
            out.append(_invalid(trade, symbol, f"arithmetic: {type(exc).__name__}"))
            continue

        if attainable is None and stopped:
            # The stop was hit on the very first bar: nothing could be taken.
            attainable = Decimal(0)
            bars_to_peak = 0

        out.append(
            Excursion(
                symbol=trade.symbol or symbol,
                opened_at=trade.opened_at,
                is_long=long,
                risk=risk,
                attainable_r=attainable,
                horizon_r=horizon,
                adverse_r=adverse,
                bars_held=bars_held,
                bars_to_peak=bars_to_peak,
                close_reason=(
                    trade.close_reason.value if trade.close_reason is not None else None
                ),
                truncated=not reached_horizon,
            )
        )

    return out


def _invalid(trade, symbol: str, reason: str) -> Excursion:
    return Excursion(
        symbol=trade.symbol or symbol,
        opened_at=trade.opened_at,
        is_long=trade.is_long,
        risk=trade.risk_per_unit,
        attainable_r=None,
        horizon_r=None,
        adverse_r=None,
        bars_held=0,
        bars_to_peak=None,
        close_reason=(
            trade.close_reason.value if trade.close_reason is not None else None
        ),
        invalid_reason=reason,
    )


# ---------------------------------------------------------------------------
# Distribution
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Distribution:
    """Sample quantiles. An empty sample means no answer, not zeros."""

    n: int
    p10: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    mean: float | None
    maximum: float | None

    def describe(self) -> str:
        if self.n == 0:
            return "the sample is empty"
        return (
            f"n={self.n}  p10={self.p10:.2f}  p25={self.p25:.2f}  "
            f"median={self.median:.2f}  p75={self.p75:.2f}  "
            f"p90={self.p90:.2f}  mean={self.mean:.2f}  max={self.maximum:.2f}"
        )


def distribution(values: list[float]) -> Distribution:
    """Quantiles by the inclusive method: on samples of tens of observations
    the exclusive variant discards the tails, and the tails are what interests us."""
    n = len(values)
    if n == 0:
        return Distribution(0, None, None, None, None, None, None, None)
    if n == 1:
        only = values[0]
        return Distribution(1, only, only, only, only, only, only, only)

    ordered = sorted(values)
    cuts = statistics.quantiles(ordered, n=100, method="inclusive")
    return Distribution(
        n=n,
        p10=cuts[9],
        p25=cuts[24],
        median=statistics.median(ordered),
        p75=cuts[74],
        p90=cuts[89],
        mean=statistics.fmean(ordered),
        maximum=ordered[-1],
    )


#: Target grid for the attainability table. A 0.25R step: anything finer
#: makes no sense with samples of tens of trades.
TARGET_GRID: tuple[float, ...] = (
    0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0,
)


def attainability(
    values: list[float], grid: tuple[float, ...] = TARGET_GRID
) -> list[tuple[float, int, float]]:
    """Share of trades in which the move reached target T.

    This is exactly the target hit rate from P-005, exact rather than an estimate:
    an exit at target T does not change whether the move reached T, so
    there is no need to recompute the run for each target.
    """
    n = len(values)
    rows: list[tuple[float, int, float]] = []
    for target in grid:
        hits = sum(1 for v in values if v >= target)
        rows.append((target, hits, hits / n if n else 0.0))
    return rows


def target_at_quantile(values: list[float], quantile: float, step: float = 0.25) -> float | None:
    """Target at a given move percentile, rounded DOWN to the grid.

    Down, not to the nearest: rounding up makes the target unattainable
    for exactly those trades it was chosen by.
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        raw = ordered[0]
    else:
        cuts = statistics.quantiles(ordered, n=100, method="inclusive")
        raw = cuts[int(round(quantile * 100)) - 1]
    return max(step, (int(raw / step)) * step)


__all__ = [
    "TARGET_GRID",
    "Distribution",
    "Excursion",
    "attainability",
    "distribution",
    "measure",
    "target_at_quantile",
]
