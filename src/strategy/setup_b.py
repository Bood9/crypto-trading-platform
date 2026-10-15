"""Setup B — Trend Continuation. Spec §6.

Hypothesis: a new 4h extreme with rising OI and NEUTRAL funding means an
inflow of fresh money rather than an overheated crowd. Neutral funding is
essential here — it distinguishes a fresh trend from the same overheated
positioning that Setup A is built on.

All parameters are fixed in the config before the first run. Tweaking them
for out-of-sample is forbidden (spec §16); the OOS sample is touched once.

The signal function receives a slice of history ending at the current bar
and physically cannot see the future (see backtest/engine.py).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest.engine import MarketBar, Signal
from src.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class SetupBParams:
    """Parameters from config/settings.yaml, section strategy.setup_b."""

    breakout_lookback: int
    require_oi_rising: bool
    funding_min_percentile: float
    funding_max_percentile: float
    funding_percentile_lookback_days: int
    stop_lookback_bars: int
    target_r: Decimal
    time_stop_hours: int

    @classmethod
    def from_config(cls, cfg: dict) -> SetupBParams:
        return cls(
            breakout_lookback=int(cfg.get("stop_lookback_bars", 20)),
            require_oi_rising=bool(cfg["require_oi_rising"]),
            funding_min_percentile=float(cfg["funding_min_percentile"]),
            funding_max_percentile=float(cfg["funding_max_percentile"]),
            funding_percentile_lookback_days=int(
                cfg.get("funding_percentile_lookback_days", 30)
            ),
            stop_lookback_bars=int(cfg["stop_lookback_bars"]),
            target_r=Decimal(str(cfg["target_r"])),
            time_stop_hours=int(cfg["time_stop_hours"]),
        )


@dataclass
class SymbolContext:
    """OI and funding series for a single symbol.

    Kept sorted by time: lookup is binary, otherwise on two years of history
    checking every bar would degenerate into a quadratic scan.
    """

    oi_ts: list[datetime]
    oi_values: list[Decimal]
    funding_ts: list[datetime]
    funding_rates: list[Decimal]

    def oi_at(self, moment: datetime) -> Decimal | None:
        """The last OI value NO LATER than the moment.

        Strictly no later: taking the nearest value in general would mean
        looking ahead by up to one interval.
        """
        idx = bisect_right(self.oi_ts, moment) - 1
        return self.oi_values[idx] if idx >= 0 else None

    def funding_window(self, moment: datetime, days: int) -> list[Decimal]:
        """Funding rates over the window ending at the moment."""
        start = moment - timedelta(days=days)
        lo = bisect_right(self.funding_ts, start)
        hi = bisect_right(self.funding_ts, moment)
        return self.funding_rates[lo:hi]

    def latest_funding(self, moment: datetime) -> Decimal | None:
        idx = bisect_right(self.funding_ts, moment) - 1
        return self.funding_rates[idx] if idx >= 0 else None


def percentile_rank(values: list[Decimal], target: Decimal) -> float | None:
    """Position of a value within a sample, in percent.

    None for an empty or degenerate sample: substituting 50 would pass
    "no data" off as "exactly in the middle" (P-001).
    """
    if len(values) < 2:
        return None
    below = sum(1 for v in values if v < target)
    equal = sum(1 for v in values if v == target)
    return (below + equal / 2) / len(values) * 100


def is_oi_rising(context: SymbolContext, moment: datetime, lookback: timedelta) -> bool | None:
    """Whether OI is rising over the window ending at the moment.

    None means there is nothing to judge by — data is missing at one end of
    the window. A missing answer is not equated with "not rising": the
    calling code decides that explicitly.
    """
    now_value = context.oi_at(moment)
    past_value = context.oi_at(moment - lookback)
    if now_value is None or past_value is None or past_value <= 0:
        return None
    return now_value > past_value


def make_signal_fn(
    context: SymbolContext, params: SetupBParams
) -> callable:
    """Builds the signal function for a single symbol.

    A closure over the context rather than global state: the engine calls it
    for every bar, and passing the OI and funding series as arguments on every
    call costs more than capturing them once.
    """
    oi_lookback = timedelta(hours=params.time_stop_hours)

    def signal_fn(window: list[MarketBar]) -> Signal | None:
        need = max(params.breakout_lookback, params.stop_lookback_bars) + 1
        if len(window) < need:
            return None

        current = window[-1]
        prior = window[-(params.breakout_lookback + 1) : -1]
        if not prior:
            return None

        highest = max(b.high for b in prior)
        lowest = min(b.low for b in prior)

        # 1. Trigger: a new 4h extreme.
        if current.close > highest:
            is_long = True
        elif current.close < lowest:
            is_long = False
        else:
            return None

        # 2. Confirmation: OI is rising.
        # P-001: an unknown OI state is no reason to let the signal through.
        # No data means no confirmation.
        if params.require_oi_rising:
            rising = is_oi_rising(context, current.ts, oi_lookback)
            if rising is not True:
                return None

        # 3. Filter: funding in the neutral zone.
        # This is exactly what distinguishes a fresh trend from the overheated
        # crowd that Setup A is built on.
        rates = context.funding_window(
            current.ts, params.funding_percentile_lookback_days
        )
        latest = context.latest_funding(current.ts)
        if latest is None:
            return None
        rank = percentile_rank(rates, latest)
        if rank is None:
            return None
        if not (params.funding_min_percentile <= rank <= params.funding_max_percentile):
            return None

        # 4. Stop beyond the structural level.
        structure = window[-(params.stop_lookback_bars + 1) : -1]
        stop = min(b.low for b in structure) if is_long else max(b.high for b in structure)

        entry = current.close
        risk = abs(entry - stop)
        if risk <= 0:
            return None

        target = entry + risk * params.target_r if is_long else entry - risk * params.target_r
        return Signal(is_long=is_long, entry=entry, stop=stop, target=target)

    return signal_fn
