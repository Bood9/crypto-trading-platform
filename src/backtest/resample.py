"""Daily bars from 4-hour ones. DEC-033.

The day is UTC, as on the exchange. A bar is stamped with the start of the day,
like everything in the system: the decision is made at the close, that is, at
00:00 of the next day.

**Incomplete days are discarded, not filled in** (P-001). A day
missing even one 4h bar would give a wrong high,
low or close, and both ATR and the window return would be computed
from an invented price. Collection gaps are single and rare (0.07%,
see indicators.py), so a discarded day is the lesser evil.

The same rule automatically cuts off the current, not yet finished
day: it has fewer than six bars. Otherwise the last "daily" bar
would contain the future relative to its close moment.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.backtest.engine import MarketBar

DAY = timedelta(days=1)


def to_daily(bars: list[MarketBar], interval: timedelta) -> list[MarketBar]:
    """UTC days from lower-timeframe bars, complete ones only."""
    if interval <= timedelta(0) or DAY % interval:
        raise ValueError(f"interval {interval} does not divide a day evenly")
    per_day = DAY // interval

    groups: dict[datetime, list[MarketBar]] = {}
    for bar in bars:
        day = bar.ts.replace(hour=0, minute=0, second=0, microsecond=0)
        groups.setdefault(day, []).append(bar)

    out: list[MarketBar] = []
    for day in sorted(groups):
        members = sorted(groups[day], key=lambda b: b.ts)
        expected = [day + interval * i for i in range(per_day)]
        if [b.ts for b in members] != expected:
            continue
        out.append(
            MarketBar(
                ts=day,
                open=members[0].open,
                high=max(b.high for b in members),
                low=min(b.low for b in members),
                close=members[-1].close,
                volume=sum((b.volume for b in members), members[0].volume * 0),
            )
        )
    return out


__all__ = ["DAY", "to_daily"]
