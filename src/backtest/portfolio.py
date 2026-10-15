"""Portfolio run: which of the signals could actually be TAKEN.

A per-symbol backtest answers the question "does the signal have an
edge". That is not the question money lives by.

Setup C gives 62 signals a month. With two slots and a seven-day
hold the account's throughput is about 8.5 trades a month.
So roughly every seventh one can be taken, and **which one exactly**
is decided not by the strategy but by portfolio limits. The per-symbol E
describes the population of signals; the account lives on the subsample
that the limits cut out of it.

The difference is not cosmetic and can go either way. The concurrent
position limit selects signals by arrival time, that is,
by a feature unrelated to their quality, and this dilutes the
result. The correlation limit, on the contrary, drops a second bet
on the same move and should reduce the spread.

**A replay, not a new run.** Candidates are taken from the already
computed per-symbol trades: a signal skipped by the per-symbol
run because of an open position in the SAME symbol would be rejected
by the governor with the very same MAX_POSITIONS code. The candidate set
does not change because of this, and there is no point computing the same thing twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.backtest.accounting import TradeOutcome
from src.core.models import RejectionCode
from src.risk.governor import (
    Anchors,
    GovernorLimits,
    HeldPosition,
    PortfolioState,
    TradingMode,
    check,
)
from src.risk.position_sizer import SizingInput, calculate_size

#: Correlation of a symbol pair at a moment. None means unknown (P-001).
CorrelationAt = Callable[[str, str, datetime], Awaitable[Decimal | None]]

#: MMR, lot step, minimum lot, minimum notional.
InstrumentParams = tuple[Decimal, Decimal, Decimal, Decimal]


@dataclass(frozen=True)
class Candidate:
    """A trade from the per-symbol run offered to the portfolio."""

    outcome: TradeOutcome

    @property
    def symbol(self) -> str:
        return self.outcome.trade.symbol

    @property
    def opened_at(self) -> datetime:
        return self.outcome.trade.opened_at

    @property
    def closed_at(self) -> datetime | None:
        return self.outcome.trade.closed_at


@dataclass
class PortfolioResult:
    """The account after the portfolio run."""

    initial_equity: Decimal
    equity: Decimal
    taken: list[TradeOutcome] = field(default_factory=list)
    #: Refusals by code. They show WHAT exactly cut the frequency.
    rejections: dict[str, int] = field(default_factory=dict)
    #: Equity curve points: close moment and equity after it.
    curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    max_drawdown_pct: Decimal = Decimal(0)
    candidates: int = 0
    #: How many times the week resumed after a weekly stop.
    weekly_resumes: int = 0
    final_mode: TradingMode = TradingMode.ON

    @property
    def taken_share(self) -> float:
        return len(self.taken) / self.candidates if self.candidates else 0.0

    @property
    def total_return_pct(self) -> Decimal:
        if self.initial_equity <= 0:
            return Decimal(0)
        return (self.equity - self.initial_equity) / self.initial_equity * 100


def _lookup(known: dict[str, Decimal | None]):
    """A closure over the already computed correlations.

    A separate function, not a lambda in the loop: a lambda would capture
    the variable, not its value, and on the next candidate would look
    into someone else's dictionary.
    """

    def fn(left: str, _right: str) -> Decimal | None:
        return known.get(left)

    return fn


async def replay(
    per_symbol: dict[str, list[TradeOutcome]],
    *,
    limits: GovernorLimits,
    instruments: dict[str, InstrumentParams],
    initial_equity: Decimal,
    margin_pct: Decimal,
    max_loss_pct: Decimal,
    leverage_cap: Decimal,
    min_liq_buffer_ratio: Decimal,
    correlation: CorrelationAt,
    resume_weekly_next_week: bool = True,
    tie_break_seed: int | None = None,
    max_position_pct: Decimal | None = None,
) -> PortfolioResult:
    """Chronological replay of trades through portfolio limits.

    The size is recomputed on the ACTUAL equity at the entry moment,
    not on the starting one: the per-symbol run computed each symbol
    from the same deposit, and the sum of such trades describes
    several independent accounts, not one.

    The R multiple does not depend on size (fees grow together
    with the position), so the trade result is carried over as
    `R × distance_to_stop × new_quantity`.

    **Resumption policy after a weekly stop.** It is lifted
    only manually (spec §5), and in a run there is nobody to lift it: the state
    is absorbing, and without an explicit policy the backtest measures not the
    return but the time until the first bad week. The measurement showed exactly
    that: 1036 refusals in a row after a single trigger.

    By default what a human does is modelled: a review at the start
    of the next ISO week and resumption from a new reference point.
    This is an ASSUMPTION, and it is named here, not hidden in a flag:
    a run with it describes a system with someone on duty, and `False` a system
    that was switched off and forgotten.

    **Concurrent candidates.** By default, by symbol name: that is how
    Setup C's numbers were computed, and they must not be changed retroactively.
    With `tie_break_seed`, shuffling with a fixed seed. For a
    setup where all symbols get a signal at the same daily close
    (Setup D), name order is a systematic alphabetical selection
    (DEC-033). Not by signal strength: ranking by strength would be a second
    hypothesis inside the same test.
    """
    def order(c: Candidate) -> tuple:
        if tie_break_seed is None:
            return (c.opened_at, c.symbol)
        # A hash, not random: the order must not depend on the sequence
        # in which candidates arrived at the input.
        digest = hashlib.sha256(
            f"{tie_break_seed}|{c.symbol}|{c.opened_at.isoformat()}".encode()
        ).hexdigest()
        return (c.opened_at, digest)

    candidates = sorted(
        (
            Candidate(outcome)
            for outcomes in per_symbol.values()
            for outcome in outcomes
            if outcome.valid and outcome.trade.closed_at is not None
        ),
        key=order,
    )

    result = PortfolioResult(
        initial_equity=initial_equity,
        equity=initial_equity,
        candidates=len(candidates),
    )
    if not candidates:
        return result

    state = PortfolioState.start(initial_equity, candidates[0].opened_at)
    #: symbol → (close moment, R, risk per unit, quantity)
    live: dict[str, tuple[datetime, Decimal, Decimal, Decimal]] = {}
    peak = initial_equity

    def maybe_resume(moment: datetime) -> None:
        """Resumption after a weekly stop at the start of a new week."""
        if not resume_weekly_next_week:
            return
        if state.mode is not TradingMode.OFF_WEEKLY:
            return
        fresh = Anchors.at(moment, state.equity)
        if fresh.week != state.anchors.week:
            state.mode = TradingMode.ON
            state.anchors = fresh
            result.weekly_resumes += 1

    def settle(until: datetime) -> None:
        """Close everything that finished no later than the moment."""
        nonlocal peak
        for symbol in sorted(
            (s for s, (closed, *_) in live.items() if closed <= until),
            key=lambda s: live[s][0],
        ):
            closed_at, r_multiple, risk_per_unit, qty = live.pop(symbol)
            pnl = r_multiple * risk_per_unit * qty
            state.close(symbol)
            state.apply_pnl(pnl, closed_at)
            result.equity = state.equity
            result.curve.append((closed_at, state.equity))
            peak = max(peak, state.equity)
            if peak > 0:
                drop = (peak - state.equity) / peak * 100
                result.max_drawdown_pct = max(result.max_drawdown_pct, drop)

    def deny(code: RejectionCode | None) -> None:
        key = code.value if code is not None else RejectionCode.EXCHANGE_ERROR.value
        result.rejections[key] = result.rejections.get(key, 0) + 1

    for candidate in candidates:
        trade = candidate.outcome.trade
        settle(candidate.opened_at)
        maybe_resume(candidate.opened_at)

        spec = instruments.get(candidate.symbol)
        if spec is None:
            deny(RejectionCode.EXCHANGE_ERROR)
            continue
        mmr, qty_step, min_qty, min_notional = spec

        sized = calculate_size(
            SizingInput(
                equity=state.equity,
                entry_price=trade.entry,
                stop_price=trade.stop,
                mmr=mmr,
                qty_step=qty_step,
                min_order_qty=min_qty,
                min_notional=min_notional,
                margin_pct=margin_pct,
                max_loss_pct=max_loss_pct,
                leverage_cap=leverage_cap,
                min_liq_buffer_ratio=min_liq_buffer_ratio,
                max_position_pct=max_position_pct,
            )
        )
        if not sized.approved:
            deny(sized.rejection)
            continue

        # Correlations are computed in advance: the governor is pure and synchronous,
        # while the source goes to the database. Pulling await into the check
        # would make asynchronous the code that decides whether money can
        # be spent, and thereby complicate its testing.
        known: dict[str, Decimal | None] = {}
        for held in state.positions:
            known[held] = await correlation(
                held, candidate.symbol, candidate.opened_at
            )

        verdict = check(
            state,
            limits,
            symbol=candidate.symbol,
            is_long=trade.is_long,
            margin=sized.margin,
            moment=candidate.opened_at,
            correlation=_lookup(known),
        )
        if not verdict.approved:
            deny(verdict.rejection)
            continue

        state.open(
            HeldPosition(
                symbol=candidate.symbol,
                is_long=trade.is_long,
                margin=sized.margin,
                qty=sized.qty,
                entry=trade.entry,
                opened_at=candidate.opened_at,
            )
        )
        live[candidate.symbol] = (
            trade.closed_at,
            candidate.outcome.r_multiple,
            trade.risk_per_unit,
            sized.qty,
        )
        result.taken.append(candidate.outcome)

    # Tail: everything left open is closed at its own time.
    if live:
        settle(max(closed for closed, *_ in live.values()))

    result.final_mode = state.mode
    return result


__all__ = [
    "Candidate",
    "CorrelationAt",
    "InstrumentParams",
    "PortfolioResult",
    "replay",
]
