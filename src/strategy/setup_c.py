"""Setup C — Trend Continuation, a test of the hypothesis. Spec §6, DEC-018.

Same hypothesis as Setup B: a new 4h extreme with rising OI and NEUTRAL
funding means an inflow of fresh money rather than an overheated crowd.

**B did not refute it — B did not test it** (DEC-013). The 3R target sat at
the 98.7th percentile of the setup's own move, and the maximum excursion at
p90 fell on the last bar of the window: the move was cut off by the time
stop before it had finished. What was measured was a random 48-hour hold.

C differs from B in exactly one way, and it is not the trigger but the **stop**.

B's stop sat beyond a 20-bar structural extreme, i.e. very far away. Both of
its problems followed from that at once:

* p75 of the excursion came out at 0.70R — not because the market does not
  move, but because R was huge. A 2R target with such a stop would mean a move
  of tens of percent;
* the risk manager rejected 885 signals on LIQ_BUFFER (DEC-013), and the
  remainder was skewed towards tight stops — a different population, not a
  smaller copy of the same one (measured in DEC-015: the median stop distance
  dropped from 11.83% to 8.88%).

Here the stop is set by ATR and capped from above by a hard ceiling.
The cap drops the signal ON THE SPOT instead of passing it to the risk manager:
a risk-manager rejection is invisible in the setup's logic and silently skews
the sample, while an explicit rejection is counted in the funnel under its own code.

All parameters are fixed BEFORE the run on the extended data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from src.backtest.engine import Signal
from src.backtest.funnel import Funnel, Stage
from src.core.logging import get_logger
from src.strategy.setup_a import AtrSeries, parse_timeframe
from src.strategy.setup_b import SymbolContext, is_oi_rising, percentile_rank

log = get_logger(__name__)


#: Funnel stages of Setup C. The order matches the order of the checks.
SETUP_C_STAGES: tuple[Stage, ...] = (
    Stage("bars", "bars considered"),
    Stage("warmup", "warm-up filled"),
    Stage("breakout", "new extreme"),
    Stage("oi_rising", "OI rising"),
    Stage("funding", "funding neutral"),
    Stage("atr_known", "ATR known"),
    Stage("stop_inside_cap", "stop inside the cap"),
    Stage("signal", "signal issued"),
    Stage("funded", "risk manager funded"),
    Stage("in_universe", "symbol was in the universe"),
)


@dataclass(frozen=True)
class SetupCParams:
    """Parameters from config/settings.yaml, section strategy.setup_c."""

    breakout_lookback: int
    require_oi_rising: bool
    oi_lookback_hours: int
    funding_min_percentile: float
    funding_max_percentile: float
    funding_percentile_lookback_days: int
    entry_timeframe: str
    stop_atr_mult: Decimal
    stop_atr_period: int
    #: Cap on the stop distance as a percentage of entry. DEC-013: a stop
    #: further than roughly 8% is impassable at any leverage on a $40 deposit,
    #: and such signals must be dropped here, not in the risk manager.
    max_stop_pct: Decimal
    target_r: Decimal
    time_stop_hours: int

    #: Sweeps. A knob without a sweep is a knob whose sensitivity nobody
    #: has checked (DEC-015, DEC-016).
    breakout_lookback_sensitivity: tuple[int, ...] = ()
    stop_atr_mult_sensitivity: tuple[Decimal, ...] = ()
    funding_band_sensitivity: tuple[float, ...] = ()
    time_stop_hours_sensitivity: tuple[int, ...] = ()
    target_r_sensitivity: tuple[Decimal, ...] = ()

    @property
    def entry_interval(self) -> timedelta:
        return parse_timeframe(self.entry_timeframe)

    @property
    def time_stop_bars(self) -> int:
        """Holding window length in bars of the entry timeframe."""
        return int(
            timedelta(hours=self.time_stop_hours) / self.entry_interval
        )

    @classmethod
    def from_config(cls, cfg: dict) -> SetupCParams:
        sweeps = {
            "breakout_lookback": [int(v) for v in cfg.get("breakout_lookback_sensitivity", [])],
            "stop_atr_mult": [Decimal(str(v)) for v in cfg.get("stop_atr_mult_sensitivity", [])],
            "funding_min_percentile": [
                float(v) for v in cfg.get("funding_band_sensitivity", [])
            ],
            "time_stop_hours": [int(v) for v in cfg.get("time_stop_hours_sensitivity", [])],
            "target_r": [Decimal(str(v)) for v in cfg.get("target_r_sensitivity", [])],
        }
        for knob, grid in sweeps.items():
            if not grid:
                raise ValueError(
                    f"knob strategy.setup_c.{knob} has no sweep: "
                    "nobody has checked how sensitive the result is to it"
                )
            base = type(grid[0])(str(cfg[knob]))
            if base not in grid:
                raise ValueError(
                    f"strategy.setup_c.{knob}={cfg[knob]} is missing "
                    f"from its own sweep {grid}"
                )

        return cls(
            breakout_lookback=int(cfg["breakout_lookback"]),
            require_oi_rising=bool(cfg["require_oi_rising"]),
            oi_lookback_hours=int(cfg["oi_lookback_hours"]),
            funding_min_percentile=float(cfg["funding_min_percentile"]),
            funding_max_percentile=float(cfg["funding_max_percentile"]),
            funding_percentile_lookback_days=int(cfg["funding_percentile_lookback_days"]),
            entry_timeframe=str(cfg.get("entry_timeframe", "4h")),
            stop_atr_mult=Decimal(str(cfg["stop_atr_mult"])),
            stop_atr_period=int(cfg["stop_atr_period"]),
            max_stop_pct=Decimal(str(cfg["max_stop_pct"])),
            target_r=Decimal(str(cfg["target_r"])),
            time_stop_hours=int(cfg["time_stop_hours"]),
            breakout_lookback_sensitivity=tuple(sweeps["breakout_lookback"]),
            stop_atr_mult_sensitivity=tuple(sweeps["stop_atr_mult"]),
            funding_band_sensitivity=tuple(sweeps["funding_min_percentile"]),
            time_stop_hours_sensitivity=tuple(sweeps["time_stop_hours"]),
            target_r_sensitivity=tuple(sweeps["target_r"]),
        )


def warmup_bars(params: SetupCParams) -> int:
    return max(params.breakout_lookback, params.stop_atr_period) + 1


def make_signal_fn(
    context: SymbolContext,
    params: SetupCParams,
    atr_series: AtrSeries | None = None,
    funnel: Funnel | None = None,
) -> callable:
    """Signal function for a single symbol.

    ATR arrives as a ready series on the same timeframe as the entry: unlike
    Setup A there is no timeframe mismatch here, but recomputing ATR on every
    bar is still quadratic.
    """
    oi_lookback = timedelta(hours=params.oi_lookback_hours)
    entry_interval = params.entry_interval
    need = warmup_bars(params)

    def mark(stage: str) -> None:
        if funnel is not None:
            funnel.enter(stage)

    def signal_fn(window) -> Signal | None:
        mark("bars")
        if len(window) < need:
            return None
        mark("warmup")

        current = window[-1]
        decided_at = current.ts + entry_interval

        prior = window[-(params.breakout_lookback + 1) : -1]
        if not prior:
            return None

        # 1. Trigger: a new extreme.
        if current.close > max(b.high for b in prior):
            is_long = True
        elif current.close < min(b.low for b in prior):
            is_long = False
        else:
            return None
        mark("breakout")

        # 2. Confirmation: OI is rising. P-001 — an unknown state is not
        # let through as "rising".
        if params.require_oi_rising:
            rising = is_oi_rising(context, current.ts, oi_lookback)
            if rising is not True:
                return None
        mark("oi_rising")

        # 3. Filter: funding in the neutral zone. This is exactly what
        # distinguishes a fresh trend from an overheated crowd.
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
        mark("funding")

        # 4. Stop by ATR, not beyond a structural extreme.
        #
        # The lookup moment is the CLOSE of the entry bar: candles are dated by
        # the start of the period, and a value by open time would belong to a
        # bar that is still in progress at decision time (DEC-014).
        atr_value = atr_series.at(decided_at) if atr_series is not None else None
        if atr_value is None or atr_value <= 0:
            return None
        mark("atr_known")

        entry = current.close
        if entry <= 0:
            return None
        risk = atr_value * params.stop_atr_mult
        stop = entry - risk if is_long else entry + risk
        if risk <= 0 or stop <= 0:
            return None

        # 5. Cap on the stop distance. Dropped HERE, not by the risk
        # manager: its rejection is invisible in the setup's logic and silently
        # skews the sample towards tight stops (DEC-015).
        if risk / entry * 100 > params.max_stop_pct:
            return None
        mark("stop_inside_cap")

        target = entry + risk * params.target_r if is_long else entry - risk * params.target_r
        mark("signal")
        return Signal(is_long=is_long, entry=entry, stop=stop, target=target)

    return signal_fn


__all__ = [
    "SETUP_C_STAGES",
    "SetupCParams",
    "make_signal_fn",
    "warmup_bars",
]
