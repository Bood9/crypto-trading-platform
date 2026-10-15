"""Setup G — rank momentum, market-neutral. DEC-044.

Hypothesis: coins that rose more than the rest over four weeks keep
outperforming the rest in the following week; the weakest keep lagging. Every
day the K strongest go long, the K weakest go short.

The rule was preregistered in commit aa14969 BEFORE this line was written.
Parameters are not tuned on results.

**The future is unreachable by construction.** Each symbol's return at a close
is computed by the same function as in D, through `PastView` — a slice of bars
up to and including that close. The rank at a close compares only returns at
THAT same close. The signal function asks for the rank at the close time of
the last bar of its window.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest.engine import MarketBar, PastView, Signal
from src.backtest.funnel import Funnel, Stage
from src.strategy.setup_a import AtrSeries
from src.strategy.setup_d import lookback_return

DAY = timedelta(days=1)

SETUP_G_STAGES: tuple[Stage, ...] = (
    Stage("bars", "days considered"),
    Stage("warmup", "warm-up filled"),
    Stage("ranked", "day ranked"),
    Stage("extreme_rank", "symbol among the K extremes"),
    Stage("atr_known", "ATR known"),
    Stage("stop_inside_cap", "stop inside the cap"),
    Stage("signal", "signal issued"),
    Stage("funded", "risk manager funded"),
    Stage("in_universe", "symbol was in the universe"),
)


@dataclass(frozen=True)
class SetupGParams:
    lookback_days: int
    top_k: int
    min_candidates: int
    hold_days: int
    stop_atr_mult: Decimal
    stop_atr_period: int
    max_stop_pct: Decimal

    lookback_days_sensitivity: tuple[int, ...] = ()
    top_k_sensitivity: tuple[int, ...] = ()
    hold_days_sensitivity: tuple[int, ...] = ()
    stop_atr_mult_sensitivity: tuple[Decimal, ...] = ()

    @property
    def time_stop_hours(self) -> int:
        return self.hold_days * 24

    @classmethod
    def from_config(cls, cfg: dict) -> SetupGParams:
        sweeps = {
            "lookback_days": [int(v) for v in cfg.get("lookback_days_sensitivity", [])],
            "top_k": [int(v) for v in cfg.get("top_k_sensitivity", [])],
            "hold_days": [int(v) for v in cfg.get("hold_days_sensitivity", [])],
            "stop_atr_mult": [Decimal(str(v)) for v in cfg.get("stop_atr_mult_sensitivity", [])],
        }
        for knob, grid in sweeps.items():
            if not grid:
                raise ValueError(
                    f"knob strategy.setup_g.{knob} has no sweep: "
                    "nobody has checked how sensitive the result is to it"
                )
            base = type(grid[0])(str(cfg[knob]))
            if base not in grid:
                raise ValueError(
                    f"strategy.setup_g.{knob}={cfg[knob]} is missing "
                    f"from its own sweep {grid}"
                )
        params = cls(
            lookback_days=int(cfg["lookback_days"]),
            top_k=int(cfg["top_k"]),
            min_candidates=int(cfg["min_candidates"]),
            hold_days=int(cfg["hold_days"]),
            stop_atr_mult=Decimal(str(cfg["stop_atr_mult"])),
            stop_atr_period=int(cfg["stop_atr_period"]),
            max_stop_pct=Decimal(str(cfg["max_stop_pct"])),
            lookback_days_sensitivity=tuple(sweeps["lookback_days"]),
            top_k_sensitivity=tuple(sweeps["top_k"]),
            hold_days_sensitivity=tuple(sweeps["hold_days"]),
            stop_atr_mult_sensitivity=tuple(sweeps["stop_atr_mult"]),
        )
        if params.min_candidates < 2 * max(params.top_k_sensitivity):
            raise ValueError("fewer candidates than 2K: longs and shorts would overlap")
        return params


def warmup_bars(params: SetupGParams) -> int:
    return max(params.lookback_days, params.stop_atr_period) + 1


#: Whether the symbol was in the universe at the bar's moment (the bar's month).
Membership = Callable[[str, datetime], bool]


class RankBook:
    """Who is among the K strongest and K weakest on which day.

    Built from the daily bars of all symbols at once — ranking is impossible
    otherwise — but every number in it uses only the past: the return at a
    close comes from a `PastView` slice up to that close, the rank from the
    returns at the same close.
    """

    def __init__(
        self,
        daily: Mapping[str, Sequence[MarketBar]],
        params: SetupGParams,
        member: Membership,
    ) -> None:
        self.params = params
        returns: dict[datetime, list[tuple[Decimal, str]]] = {}
        for symbol, bars in daily.items():
            for i in range(len(bars)):
                bar = bars[i]
                if not member(symbol, bar.ts):
                    continue
                r = lookback_return(PastView(bars, i + 1), params.lookback_days)
                if r is None:
                    continue
                returns.setdefault(bar.ts + DAY, []).append((r, symbol))

        self._side: dict[datetime, dict[str, int]] = {}
        k = params.top_k
        for close, pairs in returns.items():
            if len(pairs) < max(params.min_candidates, 2 * k):
                continue
            # ONE ordering for both ends: two separate sorts with equal returns
            # could put the same symbol both long and short.
            # Equal returns are broken by symbol name, deterministically.
            order = sorted(pairs, key=lambda p: (-p[0], p[1]))
            sides = {s: 1 for _, s in order[:k]}
            sides |= {s: -1 for _, s in order[-k:]}
            self._side[close] = sides

    def ranked(self, close: datetime) -> bool:
        return close in self._side

    def direction(self, symbol: str, close: datetime) -> int | None:
        """+1 — among the strongest (long), −1 — among the weakest (short)."""
        return self._side.get(close, {}).get(symbol)


def make_signal_fn(
    params: SetupGParams,
    symbol: str,
    atr_series: AtrSeries | None,
    book: RankBook | None,
    funnel: Funnel | None = None,
    *,
    coin_flip_seed: str | None = None,
) -> callable:
    """Signal function for a single symbol. `coin_flip_seed` is the null
    hypothesis: the same days and symbols, direction by coin flip (DEC-044)."""
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
        close = current.ts + DAY  # the bar is dated by the start of the day (DEC-014)
        if book is None or not book.ranked(close):
            return None
        mark("ranked")
        direction = book.direction(symbol, close)
        if direction is None:
            return None
        mark("extreme_rank")

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
    "SETUP_G_STAGES",
    "Membership",
    "RankBook",
    "SetupGParams",
    "make_signal_fn",
    "warmup_bars",
]
