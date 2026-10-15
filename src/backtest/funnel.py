"""Signal funnel: at which check candidates die.

The setup report answers the question "how many trades came out", and that
is useless when there are twenty-four of them. A different question is needed:
how many bars they came from and where the rest were filtered out. Without the
funnel, frequency calibration comes down to blind threshold search: each run
costs minutes, and the link between a threshold and the trade count is nowhere visible.

ENTRY into a stage is counted, not the drop-out from it. The difference of
neighbouring counters is the drop-out, but the counters themselves are monotone,
and any hole in the labelling shows at once: a stage cannot accept more
candidates than the previous one.

The distribution of the distance to the stop among survivors is collected separately.
Knowing how many signals were lost is not enough: what matters is that the lost
ones are not random. The risk manager rejects distant stops, and the remainder is
skewed towards narrow ones: a different population, not the same one but smaller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class Stage:
    key: str
    title: str


#: Setup A stages in check order. The enumeration is closed: a stage
#: missing here breaks the labelling instead of silently getting lost in the report.
SETUP_A_STAGES: tuple[Stage, ...] = (
    Stage("bars", "bars considered"),
    Stage("warmup", "warm-up filled"),
    Stage("oi_drop", "OI contraction over an hour"),
    Stage("atr_known", "ATR known"),
    Stage("candle_range", "candle size in ATR"),
    Stage("volume_z", "volume spike"),
    Stage("funding", "funding at an extreme"),
    Stage("cascade", "cascade recovered"),
    Stage("risk_positive", "risk positive"),
    Stage("target", "target ahead of entry"),
    Stage("signal", "signal issued"),
    # Counted below by the runner: the signal function knows neither about the
    # risk manager nor whether the symbol was in the universe that month.
    Stage("funded", "risk manager funded"),
    Stage("in_universe", "symbol was in the universe that month"),
)


@dataclass
class Funnel:
    """Stage entry counters. Decides nothing, only counts."""

    stages: tuple[Stage, ...]
    counts: dict[str, int] = field(default_factory=dict)
    #: Distance to stop in percent for signals that reached the stop.
    stop_pct: list[float] = field(default_factory=list)
    #: The same, but only for signals accepted by the risk manager.
    stop_pct_funded: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        known = {s.key for s in self.stages}
        if len(known) != len(self.stages):
            raise ValueError("stage keys repeat")
        for stage in self.stages:
            self.counts.setdefault(stage.key, 0)

    def enter(self, key: str) -> None:
        if key not in self.counts:
            raise KeyError(f"stage {key!r} is not declared in the funnel labelling")
        self.counts[key] += 1

    def record_stop(self, pct: Decimal) -> None:
        self.stop_pct.append(float(pct))

    def record_funded(self, pct: Decimal) -> None:
        self.stop_pct_funded.append(float(pct))

    def merge(self, other: Funnel) -> None:
        """Summation across symbols. The stages must match."""
        if tuple(s.key for s in self.stages) != tuple(s.key for s in other.stages):
            raise ValueError("the funnels are labelled differently and cannot be added")
        for key, value in other.counts.items():
            self.counts[key] += value
        self.stop_pct.extend(other.stop_pct)
        self.stop_pct_funded.extend(other.stop_pct_funded)

    def rows(self) -> list[tuple[str, int, float | None, float | None]]:
        """Stage, counter, share of the start, share of the previous stage."""
        out: list[tuple[str, int, float | None, float | None]] = []
        first: int | None = None
        previous: int | None = None
        for stage in self.stages:
            n = self.counts[stage.key]
            if first is None:
                first = n
            share = n / first if first else None
            step = n / previous if previous else None
            out.append((stage.title, n, share, step))
            previous = n
        return out

    def is_monotone(self) -> bool:
        """Counters may only decrease. A violation is a labelling defect,
        not a property of the data."""
        values = [self.counts[s.key] for s in self.stages]
        return all(a >= b for a, b in zip(values, values[1:], strict=False))


def make_funnel(stages: tuple[Stage, ...] = SETUP_A_STAGES) -> Funnel:
    return Funnel(stages=stages)


__all__ = ["SETUP_A_STAGES", "Funnel", "Stage", "make_funnel"]
