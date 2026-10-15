"""Trade accounting: R multiples and equity accumulation.

Separated from the strategy engine deliberately. The arithmetic here must
survive a collapse like LUNA (the price moves by orders of magnitude, some bars
are missing, a position may stay unclosed), and it is easier to test
in isolation.

The module's main rule: **what cannot be computed is not replaced with zero**.
A trade with an undefined R is not counted as a trade with R = 0: it is
flagged and excluded from the statistics, otherwise one degenerate position
would silently shift the averages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum


class CloseReason(StrEnum):
    STOP = "STOP"
    TARGET = "TARGET"
    TIME_STOP = "TIME_STOP"
    BREAKEVEN = "BREAKEVEN"
    LIQUIDATION = "LIQUIDATION"
    #: The stream's data ran out while a position is open. Not an exit by the
    #: strategy's rule but a cut-off history; see DATA_END in the report.
    DATA_END = "DATA_END"


@dataclass(frozen=True)
class Trade:
    symbol: str
    opened_at: datetime
    closed_at: datetime | None
    is_long: bool

    entry: Decimal
    stop: Decimal
    exit_price: Decimal | None
    qty: Decimal
    notional: Decimal
    margin: Decimal

    close_reason: CloseReason | None = None
    fees: Decimal = Decimal(0)
    funding_paid: Decimal = Decimal(0)
    #: Target level from the signal. An intent, not the fact of execution:
    #: needed so the report can show AT WHAT distance the target stood
    #: without rebuilding the signals. Optional: trades from fixtures
    #: and stress tests do not set it.
    target: Decimal | None = None

    @property
    def risk_per_unit(self) -> Decimal:
        """Distance to the stop in price: the R denominator."""
        return abs(self.entry - self.stop)

    @property
    def open(self) -> bool:
        return self.exit_price is None


@dataclass(frozen=True)
class TradeOutcome:
    trade: Trade
    pnl: Decimal | None
    r_multiple: Decimal | None
    #: Why the result cannot be computed. Empty for normal trades.
    invalid_reason: str | None = None

    @property
    def valid(self) -> bool:
        return self.r_multiple is not None and self.invalid_reason is None


def evaluate(trade: Trade) -> TradeOutcome:
    """Computes the PnL and R multiple of one trade.

    Returns `invalid_reason` instead of numbers where the result is
    undefined. Silently substituting zero is most dangerous here:
    an unclosed position in the LUNA collapse would give a "breakeven trade"
    and inflate the total.
    """
    if trade.open:
        return TradeOutcome(trade, None, None, "position not closed")

    risk = trade.risk_per_unit
    if risk <= 0:
        return TradeOutcome(trade, None, None, "stop equals the entry price")
    if not risk.is_finite():
        return TradeOutcome(trade, None, None, "non-numeric distance to stop")

    try:
        direction = Decimal(1) if trade.is_long else Decimal(-1)
        gross = (trade.exit_price - trade.entry) * direction * trade.qty
        pnl = gross - trade.fees - trade.funding_paid
        risk_amount = risk * trade.qty
        if risk_amount <= 0 or not risk_amount.is_finite():
            return TradeOutcome(trade, None, None, "zero risk in money")
        r = pnl / risk_amount
    except (InvalidOperation, ZeroDivisionError, OverflowError) as exc:
        return TradeOutcome(trade, None, None, f"arithmetic: {type(exc).__name__}")

    if not pnl.is_finite() or not r.is_finite():
        return TradeOutcome(trade, None, None, "non-numeric result")

    return TradeOutcome(trade, pnl, r)


@dataclass
class EquityCurve:
    """Equity accumulation from trade to trade.

    The peak is tracked along the way: computing the drawdown afterwards from an
    array is more expensive and easier to get wrong at the edges.
    """

    initial: Decimal
    equity: Decimal = field(init=False)
    peak: Decimal = field(init=False)
    max_drawdown_pct: Decimal = field(default=Decimal(0))
    points: list[tuple[datetime | None, Decimal]] = field(default_factory=list)
    skipped: list[TradeOutcome] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.initial <= 0:
            raise ValueError("initial equity must be positive")
        self.equity = self.initial
        self.peak = self.initial
        self.points.append((None, self.initial))

    def add(self, outcome: TradeOutcome) -> None:
        """Records a trade. Invalid ones are set aside, not zeroed."""
        if not outcome.valid or outcome.pnl is None:
            self.skipped.append(outcome)
            return

        self.equity += outcome.pnl
        # Equity may go negative on paper if costs exceeded the
        # balance. A real negative account does not exist, but
        # clipping here is not allowed: it would hide ruin.
        self.peak = max(self.peak, self.equity)
        if self.peak > 0:
            drawdown = (self.peak - self.equity) / self.peak * 100
            self.max_drawdown_pct = max(self.max_drawdown_pct, drawdown)
        self.points.append((outcome.trade.closed_at, self.equity))

    @property
    def total_return_pct(self) -> Decimal:
        return (self.equity - self.initial) / self.initial * 100

    @property
    def ruined(self) -> bool:
        """The account is exhausted. With a $40 deposit this is not an abstraction."""
        return self.equity <= 0


@dataclass(frozen=True)
class Statistics:
    trades: int
    wins: int
    losses: int
    expectancy_r: Decimal | None
    avg_win_r: Decimal | None
    avg_loss_r: Decimal | None
    profit_factor: Decimal | None
    skipped: int

    @property
    def win_rate(self) -> Decimal | None:
        if self.trades == 0:
            return None
        return Decimal(self.wins) / Decimal(self.trades) * 100


def summarize(outcomes: list[TradeOutcome]) -> Statistics:
    """Summary over trades.

    Invalid ones are counted separately and do NOT enter the averages. Their
    number is a signal in itself: many skips mean the engine often
    ran into degenerate data.
    """
    valid = [o for o in outcomes if o.valid and o.r_multiple is not None]
    skipped = len(outcomes) - len(valid)

    if not valid:
        return Statistics(0, 0, 0, None, None, None, None, skipped)

    rs = [o.r_multiple for o in valid]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]

    expectancy = sum(rs) / Decimal(len(rs))
    avg_win = sum(wins) / Decimal(len(wins)) if wins else None
    avg_loss = sum(losses) / Decimal(len(losses)) if losses else None

    gross_win = sum(wins) if wins else Decimal(0)
    gross_loss = abs(sum(losses)) if losses else Decimal(0)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else None

    return Statistics(
        trades=len(valid),
        wins=len(wins),
        losses=len(losses),
        expectancy_r=expectancy,
        avg_win_r=avg_win,
        avg_loss_r=avg_loss,
        profit_factor=profit_factor,
        skipped=skipped,
    )
