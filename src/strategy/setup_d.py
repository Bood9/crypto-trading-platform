"""Setup D — time-series momentum on daily bars. DEC-033.

Hypothesis: the sign of the return over the last four weeks predicts the
sign of the return over the next week.

This is an explicit bet on market direction over the holding window. Setup C
made it implicitly (DEC-032); here it is the hypothesis itself, and the null
hypothesis is built exactly for it: the same entries and exits, direction by coin flip.

The rule was preregistered in commit 121349f BEFORE this line was written.
Parameters are not tuned on results.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.backtest.engine import Signal
from src.backtest.funnel import Funnel, Stage
from src.strategy.setup_a import AtrSeries

DAY = timedelta(days=1)


SETUP_D_STAGES: tuple[Stage, ...] = (
    Stage("bars", "days considered"),
    Stage("warmup", "warm-up filled"),
    Stage("lookback_known", "return over the window known"),
    Stage("signal_nonzero", "sign is non-zero"),
    Stage("atr_known", "ATR known"),
    Stage("stop_inside_cap", "stop inside the cap"),
    Stage("signal", "signal issued"),
    Stage("funded", "risk manager funded"),
    Stage("in_universe", "symbol was in the universe"),
)


@dataclass(frozen=True)
class SetupDParams:
    lookback_days: int
    hold_days: int
    stop_atr_mult: Decimal
    stop_atr_period: int
    max_stop_pct: Decimal

    lookback_days_sensitivity: tuple[int, ...] = ()
    hold_days_sensitivity: tuple[int, ...] = ()
    stop_atr_mult_sensitivity: tuple[Decimal, ...] = ()

    @property
    def time_stop_hours(self) -> int:
        return self.hold_days * 24

    @property
    def entry_interval(self) -> timedelta:
        return DAY

    @classmethod
    def from_config(cls, cfg: dict) -> SetupDParams:
        sweeps = {
            "lookback_days": [int(v) for v in cfg.get("lookback_days_sensitivity", [])],
            "hold_days": [int(v) for v in cfg.get("hold_days_sensitivity", [])],
            "stop_atr_mult": [Decimal(str(v)) for v in cfg.get("stop_atr_mult_sensitivity", [])],
        }
        for knob, grid in sweeps.items():
            if not grid:
                raise ValueError(
                    f"knob strategy.setup_d.{knob} has no sweep: "
                    "nobody has checked how sensitive the result is to it"
                )
            base = type(grid[0])(str(cfg[knob]))
            if base not in grid:
                raise ValueError(
                    f"strategy.setup_d.{knob}={cfg[knob]} is missing "
                    f"from its own sweep {grid}"
                )
        return cls(
            lookback_days=int(cfg["lookback_days"]),
            hold_days=int(cfg["hold_days"]),
            stop_atr_mult=Decimal(str(cfg["stop_atr_mult"])),
            stop_atr_period=int(cfg["stop_atr_period"]),
            max_stop_pct=Decimal(str(cfg["max_stop_pct"])),
            lookback_days_sensitivity=tuple(sweeps["lookback_days"]),
            hold_days_sensitivity=tuple(sweeps["hold_days"]),
            stop_atr_mult_sensitivity=tuple(sweeps["stop_atr_mult"]),
        )


def warmup_bars(params: SetupDParams) -> int:
    return max(params.lookback_days, params.stop_atr_period) + 1


def lookback_return(window, days: int) -> Decimal | None:
    """Return from the close of the day `days` ago to the current close.

    By TIME, not by bar index: a dropped incomplete day (resample) would shift
    the index, and "28 bars ago" would turn out to be 29 days. No bar exactly
    on the required date means no answer (P-001), not the neighbouring bar.
    """
    current = window[-1]
    wanted = current.ts - DAY * days
    for i in range(len(window) - 2, -1, -1):
        bar = window[i]
        if bar.ts == wanted:
            if bar.close <= 0:
                return None
            return current.close / bar.close - 1
        if bar.ts < wanted:
            return None
    return None


def make_signal_fn(
    params: SetupDParams,
    atr_series: AtrSeries | None = None,
    funnel: Funnel | None = None,
    *,
    coin_flip_seed: str | None = None,
) -> callable:
    """Signal function for a single symbol.

    `coin_flip_seed` is the DEC-033 null hypothesis: the same entries and exits,
    with direction chosen by coin flip instead of the sign of the return. The
    generator is deterministic in the seed: otherwise a repeat run would give a
    different number, and there would be nothing to compare against.
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
        past = lookback_return(window, params.lookback_days)
        if past is None:
            return None
        mark("lookback_known")
        if past == 0:
            return None
        mark("signal_nonzero")

        is_long = past > 0
        if rng is not None:
            is_long = rng.random() < 0.5

        # ATR is looked up at the day's CLOSE: the bar is dated by its start (DEC-014).
        atr_value = (
            atr_series.at(current.ts + DAY) if atr_series is not None else None
        )
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
        # There is no target: exit by time only — that is the hypothesis itself.
        return Signal(is_long=is_long, entry=entry, stop=stop, target=None)

    return signal_fn


__all__ = [
    "SETUP_D_STAGES",
    "SetupDParams",
    "lookback_return",
    "make_signal_fn",
    "warmup_bars",
]
