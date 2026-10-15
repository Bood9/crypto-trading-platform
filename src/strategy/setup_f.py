"""Setup F — funding skew, against the crowd. DEC-042.

Hypothesis: after an extreme funding skew the price over the next few days
moves against the crowd. Longs paying a lot — short; negative funding — long.

The rule was preregistered in commit 6bcaa04 BEFORE this line was written.
Parameters are not tuned on results.

**The future is unreachable by construction.** The skew measure at a daily
close is computed only from accruals stamped no later than that close (`bisect`
by time), the percentile only from values at EARLIER closes. The signal
function receives past bars through `PastView` and asks for the measure at the
close time of the last bar — there is nothing later for it to ask about.
"""

from __future__ import annotations

import random
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest.engine import Signal
from src.backtest.funnel import Funnel, Stage
from src.strategy.setup_a import AtrSeries

DAY = timedelta(days=1)
EIGHT_HOURS = timedelta(hours=8)


SETUP_F_STAGES: tuple[Stage, ...] = (
    Stage("bars", "days considered"),
    Stage("warmup", "warm-up filled"),
    Stage("pressure_known", "funding skew known"),
    Stage("extreme", "skew is extreme"),
    Stage("atr_known", "ATR known"),
    Stage("stop_inside_cap", "stop inside the cap"),
    Stage("signal", "signal issued"),
    Stage("funded", "risk manager funded"),
    Stage("in_universe", "symbol was in the universe"),
)


@dataclass(frozen=True)
class SetupFParams:
    funding_window_days: int
    history_days: int
    min_history: int
    extreme_pct: Decimal
    base_rate_8h: Decimal
    min_coverage: Decimal
    hold_days: int
    stop_atr_mult: Decimal
    stop_atr_period: int
    max_stop_pct: Decimal

    funding_window_days_sensitivity: tuple[int, ...] = ()
    extreme_pct_sensitivity: tuple[Decimal, ...] = ()
    hold_days_sensitivity: tuple[int, ...] = ()
    stop_atr_mult_sensitivity: tuple[Decimal, ...] = ()

    @property
    def time_stop_hours(self) -> int:
        return self.hold_days * 24

    @property
    def window(self) -> timedelta:
        return DAY * self.funding_window_days

    @classmethod
    def from_config(cls, cfg: dict) -> SetupFParams:
        sweeps = {
            "funding_window_days": [int(v) for v in cfg.get("funding_window_days_sensitivity", [])],
            "extreme_pct": [Decimal(str(v)) for v in cfg.get("extreme_pct_sensitivity", [])],
            "hold_days": [int(v) for v in cfg.get("hold_days_sensitivity", [])],
            "stop_atr_mult": [Decimal(str(v)) for v in cfg.get("stop_atr_mult_sensitivity", [])],
        }
        for knob, grid in sweeps.items():
            if not grid:
                raise ValueError(
                    f"knob strategy.setup_f.{knob} has no sweep: "
                    "nobody has checked how sensitive the result is to it"
                )
            base = type(grid[0])(str(cfg[knob]))
            if base not in grid:
                raise ValueError(
                    f"strategy.setup_f.{knob}={cfg[knob]} is missing "
                    f"from its own sweep {grid}"
                )
        return cls(
            funding_window_days=int(cfg["funding_window_days"]),
            history_days=int(cfg["history_days"]),
            min_history=int(cfg["min_history"]),
            extreme_pct=Decimal(str(cfg["extreme_pct"])),
            base_rate_8h=Decimal(str(cfg["base_rate_8h"])),
            min_coverage=Decimal(str(cfg["min_coverage"])),
            hold_days=int(cfg["hold_days"]),
            stop_atr_mult=Decimal(str(cfg["stop_atr_mult"])),
            stop_atr_period=int(cfg["stop_atr_period"]),
            max_stop_pct=Decimal(str(cfg["max_stop_pct"])),
            funding_window_days_sensitivity=tuple(sweeps["funding_window_days"]),
            extreme_pct_sensitivity=tuple(sweeps["extreme_pct"]),
            hold_days_sensitivity=tuple(sweeps["hold_days"]),
            stop_atr_mult_sensitivity=tuple(sweeps["stop_atr_mult"]),
        )


def warmup_bars(params: SetupFParams) -> int:
    """Bars without which a signal is impossible: ATR. Funding history
    is checked separately — it has its own calendar."""
    return params.stop_atr_period + 1


@dataclass(frozen=True)
class FundingTape:
    """Raw accruals of a single symbol by time.

    Raw, not normalised: when the interval changes, an average over `rate_8h`
    records would overweight hourly accruals eightfold (DEC-042).
    """

    ts: list[datetime]
    rates: list[Decimal]
    interval_minutes: list[int]

    def pressure(
        self, close: datetime, window: timedelta, min_coverage: Decimal
    ) -> Decimal | None:
        """Average 8-hour long payment over the window ending at `close`.

        Accruals in (close − window, close]. Window coverage below
        `min_coverage` means unknown (P-001), not "zero".
        """
        hi = bisect_right(self.ts, close)
        lo = bisect_right(self.ts, close - window)
        if hi <= lo:
            return None
        covered = sum(self.interval_minutes[lo:hi])
        need = window.total_seconds() / 60
        if Decimal(covered) < min_coverage * Decimal(need):
            return None
        total = sum(self.rates[lo:hi], Decimal(0))
        return total * Decimal(EIGHT_HOURS.total_seconds()) / Decimal(window.total_seconds())


class PressureSeries:
    """The skew measure and its extremeness at every daily close.

    The value at a close is computed from accruals no later than that close,
    the percentile from values at earlier closes. Lookup is by close
    time.
    """

    def __init__(self, tape: FundingTape, closes: list[datetime], params: SetupFParams) -> None:
        self.params = params
        self._values: dict[datetime, Decimal | None] = {
            c: tape.pressure(c, params.window, params.min_coverage) for c in sorted(closes)
        }
        self._order = sorted(self._values)

    def value(self, close: datetime) -> Decimal | None:
        return self._values.get(close)

    def direction(self, close: datetime) -> int | None:
        """+1 — long (crowd is short), −1 — short (crowd is long), None — no skew."""
        p = self.params
        current = self._values.get(close)
        if current is None:
            return None
        i = self._order.index(close)
        start = close - DAY * p.history_days
        past = [
            v for c in self._order[:i]
            if c >= start and (v := self._values[c]) is not None
        ]
        if len(past) < p.min_history:
            return None
        share = Decimal(1) - p.extreme_pct / 100
        below = Decimal(sum(1 for v in past if v < current)) / len(past)
        above = Decimal(sum(1 for v in past if v > current)) / len(past)
        if below >= share and current > p.base_rate_8h:
            return -1
        if above >= share and current < 0:
            return 1
        return None


def make_signal_fn(
    params: SetupFParams,
    atr_series: AtrSeries | None,
    pressure: PressureSeries | None,
    funnel: Funnel | None = None,
    *,
    coin_flip_seed: str | None = None,
) -> callable:
    """Signal function for a single symbol.

    `coin_flip_seed` is the null hypothesis: the same entry days, the same exits,
    direction by coin flip (DEC-042).
    """
    need = warmup_bars(params)
    rng = random.Random(coin_flip_seed) if coin_flip_seed is not None else None

    def mark(stage: str) -> None:
        if funnel is not None:
            funnel.enter(stage)

    def signal_fn(window) -> Signal | None:
        mark("bars")
        if len(window) < need:
            return None
        mark("warmup")

        current = window[-1]
        # The bar is dated by the start of the day; the decision is at its close (DEC-014).
        close = current.ts + DAY
        if pressure is None or pressure.value(close) is None:
            return None
        mark("pressure_known")
        direction = pressure.direction(close)
        if direction is None:
            return None
        mark("extreme")

        is_long = direction > 0
        if rng is not None:
            is_long = rng.random() < 0.5

        atr_value = atr_series.at(close) if atr_series is not None else None
        if atr_value is None or atr_value <= 0:
            return None
        mark("atr_known")

        entry = current.close
        if entry <= 0:
            return None
        risk = atr_value * params.stop_atr_mult
        if risk <= 0 or risk / entry * 100 > params.max_stop_pct:
            return None
        stop = entry - risk if is_long else entry + risk
        if stop <= 0:
            return None
        mark("stop_inside_cap")

        mark("signal")
        return Signal(is_long=is_long, entry=entry, stop=stop, target=None)

    return signal_fn


__all__ = [
    "SETUP_F_STAGES",
    "FundingTape",
    "PressureSeries",
    "SetupFParams",
    "make_signal_fn",
    "warmup_bars",
]
