"""Portfolio risk. Spec §5.

The sizer (`position_sizer.py`) answers the question "what size to enter
THIS trade with". This module answers a different one: "whether to enter at
all, given everything already open and everything already lost today".

The split is not a matter of style. The sizer knows nothing about the
portfolio and so would just as readily approve two trades that are flawless
on their own but together make up one bet of double size. P-003 warns
against exactly this at the level of setup logic — here it is the same thing
at the level of positions.

The module is pure: no network, no DB, no clock. The current time and
correlations come in as arguments. The reason is the same as for the sizer:
this is the code that stands between a signal and money, and it must be
testable in full.

**Six rejection codes from `RejectionCode` had no implementation
until now**: MAX_POSITIONS, MAX_MARGIN, CORRELATION, DAILY_STOP,
WEEKLY_STOP, MODE_OFF. The enum declared them, the backtest counted
rejections by code, but the checks did not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from src.core.models import RejectionCode


class TradingMode(StrEnum):
    """Whether trading is permitted.

    The different ways of switching off differ in how they are switched
    back on, and a single flag cannot replace them: the daily limit lifts
    itself when the UTC day changes, the weekly one only by hand.
    """

    ON = "ON"
    #: Daily loss limit. Lifted automatically at 00:00 UTC.
    OFF_DAILY = "OFF_DAILY"
    #: Weekly limit. Lifted ONLY manually: a week of losses is
    #: a reason to look at the system, not to wait for Monday.
    OFF_WEEKLY = "OFF_WEEKLY"
    #: Switched off by a human or by the exchange-discrepancy watchdog.
    OFF_MANUAL = "OFF_MANUAL"

    @property
    def trading_allowed(self) -> bool:
        return self is TradingMode.ON


@dataclass(frozen=True)
class HeldPosition:
    """An open position as the portfolio sees it.

    Prices are needed not for accounting — `accounting` handles that — but
    for estimating margin and direction in the correlation check.
    """

    symbol: str
    is_long: bool
    margin: Decimal
    qty: Decimal
    entry: Decimal
    opened_at: datetime

    @property
    def direction(self) -> Decimal:
        return Decimal(1) if self.is_long else Decimal(-1)


@dataclass(frozen=True)
class Anchors:
    """Reference points for the daily and weekly loss.

    Equity at the start of the period is stored, not the accumulated loss:
    the loss would have to be reset by hand, and any missed reset would
    switch trading off for good. Of the two failure modes, this one is quieter.
    """

    day: date
    day_equity: Decimal
    #: ISO week: (year, number). The ISO year differs from the calendar year
    #: at the boundary — December 29 may belong to the first week
    #: of the following year, and a pair of numbers accounts for that.
    week: tuple[int, int]
    week_equity: Decimal

    @classmethod
    def at(cls, moment: datetime, equity: Decimal) -> Anchors:
        day = moment.astimezone(UTC).date()
        iso = day.isocalendar()
        return cls(
            day=day,
            day_equity=equity,
            week=(iso.year, iso.week),
            week_equity=equity,
        )


@dataclass
class PortfolioState:
    """Everything that needs to be known before deciding to open a position.

    Changed only through methods: equity and the reference points are linked,
    and editing one field while bypassing the other silently breaks the limits.
    """

    equity: Decimal
    anchors: Anchors
    mode: TradingMode = TradingMode.ON
    positions: dict[str, HeldPosition] = field(default_factory=dict)

    @classmethod
    def start(cls, equity: Decimal, moment: datetime) -> PortfolioState:
        if equity <= 0:
            raise ValueError(f"initial equity must be positive: {equity}")
        return cls(equity=equity, anchors=Anchors.at(moment, equity))

    @property
    def total_margin(self) -> Decimal:
        return sum((p.margin for p in self.positions.values()), Decimal(0))

    def day_loss_pct(self) -> Decimal:
        """Drawdown since the start of the UTC day, in percent. Negative on profit."""
        base = self.anchors.day_equity
        if base <= 0:
            return Decimal(0)
        return (base - self.equity) / base * 100

    def week_loss_pct(self) -> Decimal:
        base = self.anchors.week_equity
        if base <= 0:
            return Decimal(0)
        return (base - self.equity) / base * 100

    def roll(self, moment: datetime) -> None:
        """Move the reference points to the new day and week.

        Called before EVERY check, not on a schedule: the system may sit
        switched off for a day, and the arrival of a new day must not depend
        on whether a timer happened to be running at that moment.

        The daily ban is lifted right here. The weekly one is not: it is
        lifted only by hand.
        """
        fresh = Anchors.at(moment, self.equity)
        day_rolled = fresh.day != self.anchors.day
        week_rolled = fresh.week != self.anchors.week

        if not day_rolled and not week_rolled:
            return

        self.anchors = Anchors(
            day=fresh.day,
            day_equity=self.equity if day_rolled else self.anchors.day_equity,
            week=fresh.week,
            week_equity=self.equity if week_rolled else self.anchors.week_equity,
        )
        if day_rolled and self.mode is TradingMode.OFF_DAILY:
            self.mode = TradingMode.ON

    def apply_pnl(self, amount: Decimal, moment: datetime) -> None:
        """Change equity. Limits are checked RIGHT after.

        Checking them only before an entry would be too late: a series of
        stop-outs breaks through the daily limit between two signals, and
        the system would find out only when the next one arrived.
        """
        self.roll(moment)
        self.equity += amount

    def open(self, position: HeldPosition) -> None:
        if position.symbol in self.positions:
            raise ValueError(f"position in {position.symbol} already open")
        self.positions[position.symbol] = position

    def close(self, symbol: str) -> HeldPosition:
        if symbol not in self.positions:
            raise KeyError(f"no position in {symbol}")
        return self.positions.pop(symbol)


@dataclass(frozen=True)
class GovernorLimits:
    """Limits from config.risk. Built once at startup."""

    max_concurrent_positions: int
    max_total_margin_pct: Decimal
    max_correlated_positions: int
    correlation_threshold: Decimal
    daily_loss_limit_pct: Decimal
    weekly_loss_limit_pct: Decimal

    @classmethod
    def from_config(cls, risk) -> GovernorLimits:  # noqa: ANN001
        return cls(
            max_concurrent_positions=int(risk.max_concurrent_positions),
            max_total_margin_pct=Decimal(str(risk.max_total_margin_pct)),
            max_correlated_positions=int(risk.max_correlated_positions),
            correlation_threshold=Decimal(str(risk.correlation_threshold)),
            daily_loss_limit_pct=Decimal(str(risk.daily_loss_limit_pct)),
            weekly_loss_limit_pct=Decimal(str(risk.weekly_loss_limit_pct)),
        )


@dataclass(frozen=True)
class Verdict:
    approved: bool
    rejection: RejectionCode | None = None
    note: str | None = None

    @classmethod
    def allow(cls) -> Verdict:
        return cls(approved=True)

    @classmethod
    def deny(cls, code: RejectionCode, note: str) -> Verdict:
        return cls(approved=False, rejection=code, note=note)


#: Correlation of two symbols' returns over the window. None means
#: "unknown" and is NOT treated as zero (P-001).
CorrelationFn = Callable[[str, str], Decimal | None]


def _same_bet(
    a: HeldPosition, candidate_is_long: bool, correlation: Decimal
) -> bool:
    """Whether two positions make up one bet.

    The same direction at correlation +0.8 and opposite directions at
    −0.8 are the same thing: both positions win and lose together. The
    config says "when directions match", which describes only the first
    case; the second is just as dangerous and must be rejected just
    the same.
    """
    aligned = a.is_long == candidate_is_long
    return correlation > 0 if aligned else correlation < 0


def check(
    state: PortfolioState,
    limits: GovernorLimits,
    *,
    symbol: str,
    is_long: bool,
    margin: Decimal,
    moment: datetime,
    correlation: CorrelationFn,
) -> Verdict:
    """Whether to let a trade into the portfolio.

    The order of the checks is part of the contract, as with the sizer: the
    strictest obstacle fires, and the rejection code must point to the real
    reason, not to whichever comes first. System state first, then the
    loss limits, then the portfolio composition.
    """
    state.roll(moment)

    # 1. The system is switched off. Why it was switched off outweighs
    # everything else: when OFF, no limits are considered at all.
    if not state.mode.trading_allowed:
        code = {
            TradingMode.OFF_DAILY: RejectionCode.DAILY_STOP,
            TradingMode.OFF_WEEKLY: RejectionCode.WEEKLY_STOP,
            TradingMode.OFF_MANUAL: RejectionCode.MODE_OFF,
        }[state.mode]
        return Verdict.deny(code, f"mode {state.mode.value}")

    # 2. Loss limits. Checked BEFORE the portfolio composition: a breached
    # daily limit switches trading off rather than just rejecting the trade.
    week_loss = state.week_loss_pct()
    if week_loss >= limits.weekly_loss_limit_pct:
        state.mode = TradingMode.OFF_WEEKLY
        return Verdict.deny(
            RejectionCode.WEEKLY_STOP,
            f"weekly loss {week_loss:.2f}% >= "
            f"{limits.weekly_loss_limit_pct}%, manual re-enable only",
        )

    day_loss = state.day_loss_pct()
    if day_loss >= limits.daily_loss_limit_pct:
        state.mode = TradingMode.OFF_DAILY
        return Verdict.deny(
            RejectionCode.DAILY_STOP,
            f"daily loss {day_loss:.2f}% >= {limits.daily_loss_limit_pct}%",
        )

    # 3. Repeat on the same symbol. A second entry into the same instrument
    # is not a second position but an increase of the first, bypassing the sizer.
    if symbol in state.positions:
        return Verdict.deny(
            RejectionCode.MAX_POSITIONS, f"position in {symbol} already open"
        )

    # 4. Number of concurrent positions.
    if len(state.positions) >= limits.max_concurrent_positions:
        return Verdict.deny(
            RejectionCode.MAX_POSITIONS,
            f"open positions: {len(state.positions)}, cap "
            f"{limits.max_concurrent_positions}",
        )

    # 5. Total margin.
    if state.equity <= 0:
        return Verdict.deny(RejectionCode.INSUFFICIENT_BALANCE, "equity exhausted")
    total = state.total_margin + margin
    total_pct = total / state.equity * 100
    if total_pct > limits.max_total_margin_pct:
        return Verdict.deny(
            RejectionCode.MAX_MARGIN,
            f"margin {total_pct:.2f}% would exceed the cap "
            f"{limits.max_total_margin_pct}%",
        )

    # 6. Correlation. Unknown correlation is NOT zero (P-001):
    # we cannot confirm the bet is independent, so we do not enter.
    correlated = 0
    for held in state.positions.values():
        value = correlation(held.symbol, symbol)
        if value is None:
            return Verdict.deny(
                RejectionCode.CORRELATION,
                f"correlation {held.symbol}/{symbol} unknown — "
                "independence of the bet not confirmed",
            )
        if abs(value) < limits.correlation_threshold:
            continue
        if _same_bet(held, is_long, value):
            correlated += 1
            if correlated >= limits.max_correlated_positions:
                return Verdict.deny(
                    RejectionCode.CORRELATION,
                    f"{held.symbol} and {symbol} at correlation {value:+.2f} — "
                    "one bet, not two",
                )

    return Verdict.allow()


def resume(state: PortfolioState, moment: datetime) -> PortfolioState:
    """Manual re-enable after the weekly limit.

    A separate function rather than a field assignment: re-enabling after a
    weekly drawdown is a deliberate action, and it moves the reference points
    to the current equity. Otherwise the system would switch on and
    immediately switch off again, because the loss relative to the old
    reference point has not gone anywhere.
    """
    return replace(
        state,
        mode=TradingMode.ON,
        anchors=Anchors.at(moment, state.equity),
    )


__all__ = [
    "Anchors",
    "CorrelationFn",
    "GovernorLimits",
    "HeldPosition",
    "PortfolioState",
    "TradingMode",
    "Verdict",
    "check",
    "resume",
]
