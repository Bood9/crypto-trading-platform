"""Panel computations. Pure: trades in, numbers out.

The panel shows money, and computing it in the browser would mean a
second, untested set of books. Everything the panel shows as a number
is computed here and covered by tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from src.live.store import LiveTrade

#: The forward-sample size for the decision, named in advance (DEC-041).
FORWARD_TARGET = 60


def r_multiple(trade: LiveTrade) -> Decimal | None:
    """Result in R: the trade's outcome relative to the risk to the stop at entry.

    Computed from the ACTUAL entry price the stop was set against: same
    as the backtest, where R is a fraction of the initial risk.
    """
    if trade.pnl is None:
        return None
    risk = abs(trade.entry_price - trade.stop_price) * trade.qty
    if risk <= 0:
        return None
    return trade.pnl / risk


@dataclass(frozen=True)
class Stats:
    closed: int
    wins: int
    total_pnl: Decimal
    avg_r: Decimal | None
    best: Decimal | None
    worst: Decimal | None
    max_drawdown_pct: Decimal
    forward_target: int = FORWARD_TARGET

    @property
    def win_rate(self) -> Decimal | None:
        return Decimal(self.wins) / self.closed * 100 if self.closed else None


def closed_trades(trades: list[LiveTrade]) -> list[LiveTrade]:
    """Closed trades with a known result, ordered by close time."""
    done = [t for t in trades if t.status == "closed" and t.pnl is not None and t.closed_at]
    return sorted(done, key=lambda t: (t.closed_at, t.id or 0))


def equity_curve(
    trades: list[LiveTrade], initial: Decimal, start: datetime | None
) -> list[tuple[datetime, Decimal]]:
    """Equity after each close. The first point is the book's start."""
    done = closed_trades(trades)
    first = start or (done[0].opened_at if done else None)
    points: list[tuple[datetime, Decimal]] = []
    if first is not None:
        points.append((first, initial))
    equity = initial
    for t in done:
        equity += t.pnl
        points.append((t.closed_at, equity))
    return points


def max_drawdown_pct(curve: list[tuple[datetime, Decimal]]) -> Decimal:
    peak, worst = None, Decimal(0)
    for _, value in curve:
        peak = value if peak is None else max(peak, value)
        if peak and peak > 0:
            worst = max(worst, (peak - value) / peak * 100)
    return worst


def stats(trades: list[LiveTrade], initial: Decimal, start: datetime | None) -> Stats:
    done = closed_trades(trades)
    rs = [r for r in (r_multiple(t) for t in done) if r is not None]
    pnls = [t.pnl for t in done]
    return Stats(
        closed=len(done),
        wins=sum(1 for p in pnls if p > 0),
        total_pnl=sum(pnls, Decimal(0)),
        avg_r=sum(rs, Decimal(0)) / len(rs) if rs else None,
        best=max(pnls) if pnls else None,
        worst=min(pnls) if pnls else None,
        max_drawdown_pct=max_drawdown_pct(equity_curve(trades, initial, start)),
    )


def unrealized(trade: LiveTrade, price: Decimal) -> Decimal:
    """Result of an open position at a price, without the exit fee."""
    direction = Decimal(1) if trade.is_long else Decimal(-1)
    return (price - trade.entry_price) * trade.qty * direction - trade.entry_fee


def stop_distance_pct(trade: LiveTrade, price: Decimal) -> Decimal:
    """Distance from price to the stop, in % of the current price. Negative — stop passed."""
    gap = price - trade.stop_price if trade.is_long else trade.stop_price - price
    return gap / price * 100


__all__ = [
    "FORWARD_TARGET",
    "Stats",
    "closed_trades",
    "equity_curve",
    "max_drawdown_pct",
    "r_multiple",
    "stats",
    "stop_distance_pct",
    "unrealized",
]
