"""Today's setup signals. Phase 5, DEC-041.

**The same code as in the backtest, not a retelling of it.** Daily bars are built by the
same `to_daily`, ATR by the same `build_atr_series`, the decision is made by the same
signal function through the same `PastView`. A separate "live" implementation
of the rule would inevitably drift from the tested one — and what traded would not be
what was tested. The match is pinned by a test: this module's signal
on a date equals the entry the backtest would open on that date.

**Freshness is a condition, not a wish (P-001).** The decision is made
at 00:00 UTC on the close of the past day. If the last full day
is missing from the data, collection lagged, and the signal would be computed on the
price from the day before yesterday. Such a symbol is skipped with a reason code instead of
being traded "on whatever is latest".

The module is pure: bars come in as an argument, loading from the database is separate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from src.backtest.engine import MarketBar, PastView
from src.backtest.resample import DAY, to_daily
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_d import SetupDParams, make_signal_fn, warmup_bars

FOUR_HOURS = timedelta(hours=4)


class Skip(StrEnum):
    """Why a symbol has no candidate. A closed enumeration (P-001)."""

    #: The last full day is missing from the data — collection lagged.
    STALE_DATA = "STALE_DATA"
    #: Less history than the rule needs for warm-up.
    SHORT_HISTORY = "SHORT_HISTORY"
    #: The signal rule gave nothing (zero return, stop beyond the cap, etc.).
    NO_SIGNAL = "NO_SIGNAL"


@dataclass(frozen=True)
class Candidate:
    """An entry signal that has not yet passed the Risk Manager."""

    symbol: str
    #: Date of the daily bar on whose close the decision was made.
    #: The backtest dates the trade by it too — reconciliation relies on this.
    bar_ts: datetime
    is_long: bool
    #: Daily close price — the rule's reference price.
    close: Decimal
    stop: Decimal

    @property
    def stop_fraction(self) -> Decimal:
        """Distance to the stop as a fraction of the price.

        Carried over to the execution price: the entry goes at market later than
        the close, and an absolute stop level from yesterday's price would give
        a different risk than the rule computed.
        """
        return abs(self.close - self.stop) / self.close


def decision_moment(now: datetime) -> datetime:
    """The decision moment — the last UTC midnight, no later than `now`."""
    utc = now.astimezone(UTC)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def candidate_for(
    symbol: str,
    bars_4h: Iterable[MarketBar],
    params: SetupDParams,
    moment: datetime,
) -> Candidate | Skip:
    """A candidate for the symbol at the decision moment, or a rejection reason code.

    Bars no later than the moment: everything dated after it is the future
    relative to the decision and is cut off here rather than trusted to
    the caller.
    """
    past = [b for b in bars_4h if b.ts + FOUR_HOURS <= moment]
    daily = to_daily(past, FOUR_HOURS)
    expected = moment - DAY
    if not daily or daily[-1].ts != expected:
        return Skip.STALE_DATA
    if len(daily) < warmup_bars(params):
        return Skip.SHORT_HISTORY

    atr = build_atr_series(daily, params.stop_atr_period, DAY)
    signal = make_signal_fn(params, atr)(PastView(daily, len(daily)))
    if signal is None:
        return Skip.NO_SIGNAL
    return Candidate(
        symbol=symbol,
        bar_ts=daily[-1].ts,
        is_long=signal.is_long,
        close=signal.entry,
        stop=signal.stop,
    )


def ordered(candidates: Iterable[Candidate], seed: int) -> list[Candidate]:
    """Queue order — the same hash as in the portfolio replay.

    All symbols get a signal on the same daily close, and there are fewer slots
    than candidates. Ordering by name would be alphabetical selection,
    by signal strength a second hypothesis (DEC-033). Matching
    `portfolio.replay` means the live cycle selects exactly the
    trades the backtest would select.
    """

    def key(c: Candidate) -> tuple:
        digest = hashlib.sha256(
            f"{seed}|{c.symbol}|{c.bar_ts.isoformat()}".encode()
        ).hexdigest()
        return (c.bar_ts, digest)

    return sorted(candidates, key=key)


__all__ = [
    "FOUR_HOURS",
    "Candidate",
    "Skip",
    "candidate_for",
    "decision_moment",
    "ordered",
]
