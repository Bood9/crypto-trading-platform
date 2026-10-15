"""Setup H — reversion after an extreme day. DEC-046.

Hypothesis: after a day with a move of at least k × ATR, part of the move
reverses over the next three days. Entry against the move at that day's close.

The rule was preregistered in commit f89ccd6 BEFORE this line was written.
Parameters are not tuned on results.

**The future is unreachable by construction.** The decision is made by the
`PastView` window function; the yardstick for the extreme is ATR at the
PREVIOUS close (the ATR series is keyed by close time), so the extreme day
itself does not enter its own yardstick.
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

SETUP_H_STAGES: tuple[Stage, ...] = (
    Stage("bars", "days considered"),
    Stage("warmup", "warm-up filled"),
    Stage("move_known", "day's move and yardstick known"),
    Stage("extreme", "move is extreme"),
    Stage("atr_known", "ATR for the stop known"),
    Stage("stop_inside_cap", "stop inside the cap"),
    Stage("signal", "signal issued"),
    Stage("funded", "risk manager funded"),
    Stage("in_universe", "symbol was in the universe"),
)


@dataclass(frozen=True)
class SetupHParams:
    extreme_atr_mult: Decimal
    hold_days: int
    stop_atr_mult: Decimal
    stop_atr_period: int
    max_stop_pct: Decimal

    extreme_atr_mult_sensitivity: tuple[Decimal, ...] = ()
    hold_days_sensitivity: tuple[int, ...] = ()
    stop_atr_mult_sensitivity: tuple[Decimal, ...] = ()

    @property
    def time_stop_hours(self) -> int:
        return self.hold_days * 24

    @classmethod
    def from_config(cls, cfg: dict) -> SetupHParams:
        sweeps = {
            "extreme_atr_mult": [
                Decimal(str(v)) for v in cfg.get("extreme_atr_mult_sensitivity", [])
            ],
            "hold_days": [int(v) for v in cfg.get("hold_days_sensitivity", [])],
            "stop_atr_mult": [Decimal(str(v)) for v in cfg.get("stop_atr_mult_sensitivity", [])],
        }
        for knob, grid in sweeps.items():
            if not grid:
                raise ValueError(
                    f"knob strategy.setup_h.{knob} has no sweep: "
                    "nobody has checked how sensitive the result is to it"
                )
            base = type(grid[0])(str(cfg[knob]))
            if base not in grid:
                raise ValueError(
                    f"strategy.setup_h.{knob}={cfg[knob]} is missing "
                    f"from its own sweep {grid}"
                )
        return cls(
            extreme_atr_mult=Decimal(str(cfg["extreme_atr_mult"])),
            hold_days=int(cfg["hold_days"]),
            stop_atr_mult=Decimal(str(cfg["stop_atr_mult"])),
            stop_atr_period=int(cfg["stop_atr_period"]),
            max_stop_pct=Decimal(str(cfg["max_stop_pct"])),
            extreme_atr_mult_sensitivity=tuple(sweeps["extreme_atr_mult"]),
            hold_days_sensitivity=tuple(sweeps["hold_days"]),
            stop_atr_mult_sensitivity=tuple(sweeps["stop_atr_mult"]),
        )


def warmup_bars(params: SetupHParams) -> int:
    # ATR at the previous close plus the day itself.
    return params.stop_atr_period + 2


def make_signal_fn(
    params: SetupHParams,
    atr_series: AtrSeries | None,
    funnel: Funnel | None = None,
    *,
    coin_flip_seed: str | None = None,
) -> callable:
    """Signal function for a single symbol. `coin_flip_seed` is the null
    hypothesis: the same days, direction by coin flip (DEC-046)."""
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

        current, previous = window[-1], window[-2]
        # The previous day must be exactly yesterday, otherwise the "day's move" is
        # a move over several days across a dropped incomplete day (P-001).
        if previous.ts != current.ts - DAY or previous.close <= 0:
            return None
        if atr_series is None:
            return None
        # The bar is dated by the start of the day: yesterday's close = today's start.
        yardstick = atr_series.at(current.ts)
        if yardstick is None or yardstick <= 0:
            return None
        move = current.close - previous.close
        mark("move_known")
        if abs(move) < params.extreme_atr_mult * yardstick:
            return None
        mark("extreme")

        is_long = move < 0  # against the move
        if rng is not None:
            is_long = rng.random() < 0.5

        atr_value = atr_series.at(current.ts + DAY)
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


__all__ = ["SETUP_H_STAGES", "SetupHParams", "make_signal_fn", "warmup_bars"]
