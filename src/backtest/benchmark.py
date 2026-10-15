"""Random entry as the null hypothesis.

A question that neither E nor its standard error answers:
**does the trigger do anything at all?**

A setup is not only the entry condition. It is also an ATR stop, a 2R target,
a holding window, a cap on the distance to the stop, portfolio limits
and moving to breakeven. Any of these parts can produce a
positive E on its own: for example, the construct "stop 1.5 ATR,
target 2R" in a trending market makes money with ANY entry, simply
because the distribution of moves is asymmetric.

Exactly one detail is touched here: the entry condition is replaced by a coin flip.
Everything else is the same function, the same parameters, the same engine.
If the random-entry E is indistinguishable from the setup's E, then it is
the exit construction that works, the trigger adds nothing, and no conclusions
about it can be drawn.

This is the same question Setup A burned on: there was no dose-response
dependence on trigger strength there; here its very presence is tested.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta

from src.backtest.engine import Signal
from src.strategy.setup_a import AtrSeries
from src.strategy.setup_c import SetupCParams


@dataclass(frozen=True)
class RandomEntryParams:
    """Random entry parameters.

    The signal share and long share are taken from the setup's ACTUAL run:
    the comparison makes sense only with matching frequency and matching
    directional skew. A random entry firing three times as often
    would measure a different quantity.
    """

    signal_rate: float
    long_share: float
    seed: int


def make_random_signal_fn(
    params: SetupCParams,
    random_params: RandomEntryParams,
    atr_series: AtrSeries | None = None,
    *,
    symbol: str = "",
) -> callable:
    """A signal with the same body as Setup C, but with a random entry.

    Stop, target, distance cap and the ATR reconciliation moment are the same
    and come from the same code in meaning, not rewritten here
    anew: the comparison makes sense only if exactly one detail
    differs.

    Each symbol gets its own generator, deterministic by seed:
    otherwise a repeated run would give a different answer, and there would be
    nothing to compare with.
    """
    rng = random.Random(f"{random_params.seed}:{symbol}")
    entry_interval = params.entry_interval
    need = max(params.breakout_lookback, params.stop_atr_period) + 1

    def signal_fn(window) -> Signal | None:
        if len(window) < need:
            return None
        if rng.random() >= random_params.signal_rate:
            return None

        current = window[-1]
        decided_at = current.ts + entry_interval
        atr_value = atr_series.at(decided_at) if atr_series is not None else None
        if atr_value is None or atr_value <= 0:
            return None

        entry = current.close
        if entry <= 0:
            return None
        risk = atr_value * params.stop_atr_mult
        if risk <= 0:
            return None
        if risk / entry * 100 > params.max_stop_pct:
            return None

        is_long = rng.random() < random_params.long_share
        stop = entry - risk if is_long else entry + risk
        if stop <= 0:
            return None
        target = (
            entry + risk * params.target_r
            if is_long
            else entry - risk * params.target_r
        )
        return Signal(is_long=is_long, entry=entry, stop=stop, target=target)

    return signal_fn


def observed_rate(trades: int, bars: int) -> float:
    """Share of bars on which the setup issued a signal.

    A rough estimate: the engine does not ask the strategy while a position is
    open, so the denominator is understated and the frequency overstated.
    The skew is towards a LARGER number of random entries, that is,
    towards a stricter comparison.
    """
    if bars <= 0:
        return 0.0
    return min(1.0, trades / bars)


def long_share(trades: list) -> float:
    """Share of longs among trades. Half for an empty list."""
    if not trades:
        return 0.5
    longs = sum(1 for o in trades if o.trade.is_long)
    return longs / len(trades)


def advantage_over(
    setup_rs: list[float], random_rs: list[float]
) -> tuple[float, float, float] | None:
    """Excess of the setup's E over random entry, in standard errors.

    Computed over the POOLED trades of the random runs, not
    over the spread of means between seeds. The spread between seeds
    describes only the generator's instability and ignores
    each estimate's own error: by it the excess comes out
    twice as large as it really is.

    Returns (setup E, random-entry E, z).
    """
    if len(setup_rs) < 2 or len(random_rs) < 2:
        return None
    import statistics

    e_setup = statistics.fmean(setup_rs)
    e_random = statistics.fmean(random_rs)
    se_setup = statistics.stdev(setup_rs) / len(setup_rs) ** 0.5
    se_random = statistics.stdev(random_rs) / len(random_rs) ** 0.5
    combined = (se_setup**2 + se_random**2) ** 0.5
    if combined <= 0:
        return None
    return e_setup, e_random, (e_setup - e_random) / combined


def holding_bars(params: SetupCParams) -> int:
    return int(timedelta(hours=params.time_stop_hours) / params.entry_interval)


__all__ = [
    "RandomEntryParams",
    "advantage_over",
    "holding_bars",
    "long_share",
    "make_random_signal_fn",
    "observed_rate",
]
