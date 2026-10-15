"""Minimal backtest engine: a sequential pass over the bars.

The key property is **inability to look into the future by construction**,
not by agreement. The strategy gets not the whole series but the slice
`bars[: i + 1]`, so accessing a bar that has not happened yet shows up
as an IndexError, not as a quietly inflated result.

This is not paranoia: lookahead is the most expensive backtest error and the
least visible one. The check "the result does not change when future bars are
corrupted" (spec §14) passes trivially on this construction, and that is the
whole point: the protection is in the shape of the code, not in a test.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest.accounting import (
    CloseReason,
    EquityCurve,
    Trade,
    TradeOutcome,
    evaluate,
)


@dataclass(frozen=True)
class MarketBar:
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal(0)


@dataclass(frozen=True)
class Signal:
    """Intent to open a position. Prices are levels, not the fact of execution.

    `target=None` means exit by time only (DEC-033). Not "a target somewhere
    very far away": a distant target would look like a real one in the report,
    and the target guards (P-005, P-006) would take it seriously.
    """

    is_long: bool
    entry: Decimal
    stop: Decimal
    target: Decimal | None


@dataclass
class OpenPosition:
    signal: Signal
    opened_at: datetime
    qty: Decimal
    notional: Decimal
    margin: Decimal
    liq_price: Decimal
    entry_fee: Decimal
    #: The active stop. Differs from `signal.stop` after moving it
    #: to breakeven. The R denominator does NOT change: it is computed
    #: from the original stop, otherwise the move would retroactively rewrite
    #: the risk we took at the moment of entry.
    active_stop: Decimal | None = None
    #: The stop has already been moved; it is not moved again.
    breakeven_armed: bool = False

    @property
    def stop(self) -> Decimal:
        return self.active_stop if self.active_stop is not None else self.signal.stop


def breakeven_level(
    entry: Decimal, *, is_long: bool, fee_pct: Decimal
) -> Decimal:
    """The price at which exiting closes the trade without a loss.

    "Breakeven" is computed INCLUDING FEES (spec §6): a stop exactly
    at the entry price would leave the round-trip fee unpaid, that is,
    turn a flat trade into a losing one.

    Slippage is NOT built in here: it works against us here too,
    as everywhere, and the actual exit will be slightly worse than
    breakeven. Building it into the level would mean moving the stop
    farther from the entry for the sake of a nice number in the report.
    """
    factor = fee_pct / 100 * 2
    return entry * (Decimal(1) + factor) if is_long else entry * (Decimal(1) - factor)


class PastView(Sequence):
    """A history window ending at the current bar.

    A view, not a copy. Copying the slice `bars[:i+1]` on every
    bar gives quadratic complexity: on 70 thousand 15-minute bars
    that is billions of operations and a run that never finishes.

    The guarantee that the future is invisible is fully preserved: an index
    beyond the window gives an IndexError exactly as with a truncated
    list. The protection stays in the shape of the code, not in an agreement.

    **What this guarantee does NOT cover.** It holds only the bar array.
    Series tied to TIME rather than to an index (ATR from another
    timeframe, OI, funding) reach the strategy bypassing the window, and they have
    their own boundary: a value must be fully known by the decision
    moment. Bybit candles are stamped with the period start, so
    "the last value no later than the bar time" there means the value
    of a period not yet closed. That is exactly how lookahead leaked into
    Setup A's ATR with a fully correct window (DEC-014).
    """

    __slots__ = ("_bars", "_end")

    def __init__(self, bars: Sequence[MarketBar], end: int) -> None:
        if not 0 <= end <= len(bars):
            raise ValueError(
                f"a window of {end} bars does not fit a series of length {len(bars)}"
            )
        self._bars = bars
        self._end = end  # number of visible bars

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self._end)
            return [self._bars[i] for i in range(start, stop, step)]
        if index < 0:
            index += self._end
        if not 0 <= index < self._end:
            raise IndexError(
                f"access to bar {index} beyond the visible window "
                f"({self._end} bars): an attempt to look into the future"
            )
        return self._bars[index]


#: The strategy sees only the past: a window of bars ending at the current one.
SignalFn = Callable[[Sequence[MarketBar]], Signal | None]
#: Position size from the signal and entry price. None means a risk refusal.
SizerFn = Callable[[Signal, Decimal], tuple[Decimal, Decimal, Decimal] | None]


@dataclass
class BacktestConfig:
    initial_equity: Decimal
    taker_fee_pct: Decimal
    slippage_pct: Decimal
    time_stop: timedelta
    #: How many bars the strategy needs to make any decision at all.
    warmup_bars: int = 20

    #: Move the stop to breakeven when this number of R is reached.
    #: None means off. The value comes from spec §6 (strategy.management).
    #:
    #: Off by default DELIBERATELY: enabling it changes the result
    #: of all past runs, and Setup B was rejected with specific numbers
    #: that must not be rewritten retroactively.
    breakeven_at_r: Decimal | None = None


@dataclass
class BacktestResult:
    outcomes: list[TradeOutcome] = field(default_factory=list)
    curve: EquityCurve | None = None
    #: A position left open at the end of the data. Not an exit
    #: by rule but a cut-off history.
    unclosed: int = 0
    bars_processed: int = 0

    @property
    def valid_trades(self) -> int:
        return sum(1 for o in self.outcomes if o.valid)


def _fill_price(level: Decimal, *, is_long: bool, slippage_pct: Decimal) -> Decimal:
    """Execution price with slippage AGAINST us.

    Always against: slippage in our favour is not conservatism
    but free profit that will not exist in reality.
    """
    factor = slippage_pct / 100
    return level * (Decimal(1) + factor) if is_long else level * (Decimal(1) - factor)


@dataclass(frozen=True)
class FundingSchedule:
    """Funding accruals for one symbol.

    The rates are RAW, as the exchange charges them per its interval, not
    normalized to eight hours. Normalization is needed to compare symbols
    with each other; for counting how much was charged to the account it is
    unnecessary and even dangerous: a symbol's interval changes over time,
    while normalization uses today's (DEC-025).
    """

    ts: list[datetime]
    rates: list[Decimal]

    def between(self, opened: datetime, closed: datetime) -> Decimal:
        """Sum of rates accrued STRICTLY after opening and no later than
        closing.

        Strictly after: an accrual exactly at the entry moment is charged
        to whoever held the position before it, not to us.
        """
        lo = bisect_right(self.ts, opened)
        hi = bisect_right(self.ts, closed)
        return sum(self.rates[lo:hi], Decimal(0))


def run(
    bars: Sequence[MarketBar],
    signal_fn: SignalFn,
    sizer_fn: SizerFn,
    config: BacktestConfig,
    *,
    symbol: str = "",
    funding: FundingSchedule | None = None,
) -> BacktestResult:
    """A run over one symbol.

    A position is opened on a signal at the bar close and is checked
    for exit starting from the NEXT bar: an entry and exit within one
    bar would mean knowing its future.

    `funding` is the accrual schedule. Without it the position is held
    for free, and on a window of up to a week that is a noticeable error in our
    favour: the measurement gave 0.0023R per trade with E around 0.07R (DEC-025).
    """
    result = BacktestResult()
    curve = EquityCurve(initial=config.initial_equity)
    position: OpenPosition | None = None

    fee_rate = config.taker_fee_pct / 100

    for i, bar in enumerate(bars):
        result.bars_processed += 1

        if position is not None:
            exit_price, reason = _check_exit(position, bar, config)
            if exit_price is not None:
                fill = _fill_price(
                    exit_price,
                    is_long=not position.signal.is_long,
                    slippage_pct=config.slippage_pct,
                )
                exit_fee = fill * position.qty * fee_rate
                paid = _funding_paid(position, bar.ts, funding)
                trade = Trade(
                    symbol=symbol,
                    opened_at=position.opened_at,
                    closed_at=bar.ts,
                    is_long=position.signal.is_long,
                    entry=position.signal.entry,
                    stop=position.signal.stop,
                    exit_price=fill,
                    qty=position.qty,
                    notional=position.notional,
                    margin=position.margin,
                    close_reason=reason,
                    fees=position.entry_fee + exit_fee,
                    funding_paid=paid,
                    target=position.signal.target,
                )
                outcome = evaluate(trade)
                result.outcomes.append(outcome)
                curve.add(outcome)
                position = None
            else:
                _arm_breakeven(position, bar, config)
                # A position is open: no new signals are accepted.
                continue

        if position is not None or i < config.warmup_bars:
            continue

        # Window up to and including the current bar. The future is not here
        # physically, not by convention: an index beyond the boundary gives
        # IndexError.
        window = PastView(bars, i + 1)
        signal = signal_fn(window)
        if signal is None:
            continue

        sized = sizer_fn(signal, bar.close)
        if sized is None:
            continue
        qty, notional, margin = sized
        if qty <= 0:
            continue

        fill = _fill_price(
            signal.entry, is_long=signal.is_long, slippage_pct=config.slippage_pct
        )
        position = OpenPosition(
            signal=signal,
            opened_at=bar.ts,
            qty=qty,
            notional=notional,
            margin=margin,
            liq_price=Decimal(0),
            entry_fee=fill * qty * fee_rate,
        )

    # The data ran out and a position is open. This is not an exit by the
    # strategy's rule: closing it at the last price would mean inventing
    # a trade that never happened.
    if position is not None:
        result.unclosed += 1
        trade = Trade(
            symbol=symbol,
            opened_at=position.opened_at,
            closed_at=None,
            is_long=position.signal.is_long,
            entry=position.signal.entry,
            stop=position.signal.stop,
            exit_price=None,
            qty=position.qty,
            notional=position.notional,
            margin=position.margin,
            close_reason=CloseReason.DATA_END,
            fees=position.entry_fee,
            target=position.signal.target,
        )
        outcome = evaluate(trade)
        result.outcomes.append(outcome)
        curve.add(outcome)

    result.curve = curve
    return result


def _funding_paid(
    position: OpenPosition, closed_at: datetime, funding: FundingSchedule | None
) -> Decimal:
    """How much was charged for holding. Positive means we paid.

    A long pays at a positive rate, a short receives. The sign here
    is easy to mix up, and the error looks harmless: the magnitude is small,
    but the direction flips its contribution.
    """
    if funding is None:
        return Decimal(0)
    total = funding.between(position.opened_at, closed_at)
    if total == 0:
        return Decimal(0)
    direction = Decimal(1) if position.signal.is_long else Decimal(-1)
    return total * direction * position.signal.entry * position.qty


def _check_exit(
    position: OpenPosition, bar: MarketBar, config: BacktestConfig
) -> tuple[Decimal | None, CloseReason | None]:
    """Exit check on the current bar.

    The order of checks is conservative: stop before target. Within a bar
    it is unknown what happened first, and assuming a favourable
    order would inflate the result.
    """
    sig = position.signal
    long = sig.is_long
    stop = position.stop

    stop_hit = bar.low <= stop if long else bar.high >= stop
    if stop_hit:
        reason = (
            CloseReason.BREAKEVEN if position.breakeven_armed else CloseReason.STOP
        )
        return stop, reason

    if sig.target is not None:
        target_hit = bar.high >= sig.target if long else bar.low <= sig.target
        if target_hit:
            return sig.target, CloseReason.TARGET

    if bar.ts - position.opened_at >= config.time_stop:
        return bar.close, CloseReason.TIME_STOP

    return None, None


def _arm_breakeven(
    position: OpenPosition, bar: MarketBar, config: BacktestConfig
) -> None:
    """Move the stop to breakeven if the bar reached the R threshold.

    Called AFTER the exit check, so the moved stop takes effect
    from the next bar. Within one bar it is unknown what happened
    first, reaching the threshold or returning to the entry price,
    and assuming the threshold came first would pass losing trades
    off as breakeven ones.
    """
    if config.breakeven_at_r is None or position.breakeven_armed:
        return
    sig = position.signal
    risk = abs(sig.entry - sig.stop)
    if risk <= 0:
        return

    threshold = (
        sig.entry + risk * config.breakeven_at_r
        if sig.is_long
        else sig.entry - risk * config.breakeven_at_r
    )
    reached = bar.high >= threshold if sig.is_long else bar.low <= threshold
    if not reached:
        return

    level = breakeven_level(
        sig.entry, is_long=sig.is_long, fee_pct=config.taker_fee_pct
    )
    # The stop may only be moved IN OUR FAVOUR. With a very close
    # original stop, breakeven may lie beyond it, and the move would
    # increase the risk instead of removing it.
    better = level > sig.stop if sig.is_long else level < sig.stop
    if not better:
        return
    position.active_stop = level
    position.breakeven_armed = True
