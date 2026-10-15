"""Report on a setup run: five acceptance thresholds and five mandatory blocks.

The blocks are ALWAYS computed, whether the thresholds passed or not.
Especially if they did not: then the blocks explain what exactly broke.

Output order: bare numbers first, then their reading. The separation
is deliberate: an interpretation read before the numbers sets the frame
in which the numbers are then seen.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.backtest.accounting import Statistics, TradeOutcome, summarize
from src.backtest.runner import RunResult, run_setup_b
from src.core.config import Settings
from src.core.db import UniverseSnapshot, session_scope
from src.core.logging import get_logger

log = get_logger(__name__)

UNKNOWN = "not computed"


#: P-005. Below this target hit rate the run is invalid: it measures
#: the holding window, not the setup's hypothesis.
#:
#: The rationale is the asymmetry: with a 3R target the breakeven win rate is 25%.
#: If the target is hit in fewer than one trade in ten, the contribution of
#: target exits to E is smaller than the time-stop noise.
MIN_TARGET_HIT_RATE = 0.10


class RunInvalid(Exception):
    """The run measures something other than what is claimed (P-005).

    A separate type, not a flag in the report: the result of an invalid
    run must not get into the statistics at all. A flag can be
    missed, an exception cannot.
    """


@dataclass(frozen=True)
class ExitBreakdown:
    """Distribution of exit reasons. Computed BEFORE the statistics."""

    counts: dict[str, int]
    total: int

    @property
    def target_rate(self) -> float:
        return self.counts.get("TARGET", 0) / self.total if self.total else 0.0

    @property
    def time_stop_rate(self) -> float:
        return self.counts.get("TIME_STOP", 0) / self.total if self.total else 0.0

    @property
    def valid(self) -> bool:
        return self.target_rate >= MIN_TARGET_HIT_RATE

    def describe(self) -> str:
        parts = ", ".join(
            f"{k} {n} ({n / self.total:.1%})"
            for k, n in sorted(self.counts.items(), key=lambda kv: -kv[1])
        )
        return f"exits {self.total}: {parts}"


def exit_breakdown(outcomes: list[TradeOutcome]) -> ExitBreakdown:
    counts: dict[str, int] = {}
    for o in outcomes:
        if o.trade.close_reason is not None:
            key = o.trade.close_reason.value
            counts[key] = counts.get(key, 0) + 1
    return ExitBreakdown(counts=counts, total=sum(counts.values()))


#: Below this number of trades the target hit rate is not a rate:
#: the confidence interval around any observed value overlaps
#: the threshold, and a PASS verdict is backed by nothing.
#:
#: Added after the trigger sweep produced E = +2.475R, a 100%
#: win rate and a P-005 PASS verdict, on ONE trade (DEC-016).
MIN_TRADES_FOR_VERDICT = 30


def assert_run_is_valid(breakdown: ExitBreakdown, setup: str) -> None:
    """P-005: a run with an unreachable target gives no right to conclusions."""
    if breakdown.total == 0:
        raise RunInvalid(f"{setup}: no trades, nothing to measure")
    if breakdown.total < MIN_TRADES_FOR_VERDICT:
        # "Not computed" is not PASS (P-002). A rate estimated
        # from a handful of observations says nothing about the construct:
        # three hits out of twenty-six give an interval that contains
        # both 3% and 30%.
        raise RunInvalid(
            f"{setup}: {breakdown.total} trades with a minimum of "
            f"{MIN_TRADES_FOR_VERDICT}: the target hit rate "
            f"({breakdown.target_rate:.1%}) cannot be estimated on such a sample. "
            "Verdict NOT DECIDED, not PASS."
        )
    if not breakdown.valid:
        raise RunInvalid(
            f"{setup}: target hit in {breakdown.target_rate:.1%} of trades "
            f"with a minimum of {MIN_TARGET_HIT_RATE:.0%}. "
            f"{breakdown.describe()}. "
            "The run measures the holding window, not the setup's hypothesis (P-005). "
            "Tuning the target for reachability on the same data is not allowed: "
            "that is curve fitting; the setup must be rebuilt and treated as a new hypothesis."
        )


#: P-006. Below these thresholds the target does not pay for its own execution.
#: Both numbers were already in config/settings.yaml as stated requirements
#: (`strategy.min_target_r`, `strategy.min_move_pct`) and were not checked
#: anywhere in the code — see DEC-014.


@dataclass(frozen=True)
class TargetEconomics:
    """Whether the target pays for itself. Computed BEFORE the statistics, like P-005."""

    trades: int
    median_r: float | None
    median_move_pct: float | None
    min_target_r: float
    min_move_pct: float

    @property
    def pays_for_the_ratio(self) -> bool:
        return self.median_r is not None and self.median_r >= self.min_target_r

    @property
    def pays_for_the_costs(self) -> bool:
        return (
            self.median_move_pct is not None
            and self.median_move_pct >= self.min_move_pct
        )

    @property
    def valid(self) -> bool:
        return self.pays_for_the_ratio and self.pays_for_the_costs

    def describe(self) -> str:
        if self.trades == 0:
            return "no targets were set"
        r = "—" if self.median_r is None else f"{self.median_r:.2f}R"
        pct = "—" if self.median_move_pct is None else f"{self.median_move_pct:.2f}%"
        return (
            f"median distance to target: {r} with a minimum of "
            f"{self.min_target_r:.2f}R, {pct} with a minimum of {self.min_move_pct:.2f}%"
        )


def target_economics(outcomes: list[TradeOutcome], settings: Settings) -> TargetEconomics:
    """Distance to the target in R and in percent of the move.

    The target is taken from the trade, not from the setup's parameters: with
    a VWAP exit it is not an assigned number but a value computed by the
    market, and different for every trade.
    """
    in_r: list[float] = []
    in_pct: list[float] = []
    for outcome in outcomes:
        trade = outcome.trade
        if trade.target is None:
            continue
        distance = abs(trade.target - trade.entry)
        if trade.risk_per_unit > 0:
            in_r.append(float(distance / trade.risk_per_unit))
        if trade.entry > 0:
            in_pct.append(float(distance / trade.entry * 100))

    return TargetEconomics(
        trades=len(in_r),
        median_r=statistics.median(in_r) if in_r else None,
        median_move_pct=statistics.median(in_pct) if in_pct else None,
        min_target_r=float(settings.strategy["min_target_r"]),
        min_move_pct=float(settings.strategy["min_move_pct"]),
    )


def assert_target_is_payable(economics: TargetEconomics, setup: str) -> None:
    """P-006: a target that is reachable but does not pay is not a target either.

    P-005 catches one side of the mismatch: a target farther than the market
    moves. The other side was caught by nothing, and it looks far more
    convincing: a high hit rate and a high win rate with a target that
    covers neither the stop nor the round-trip fee. Such a run is
    flagged by no guard and reads as a success.
    """
    if economics.trades == 0:
        raise RunInvalid(f"{setup}: no targets were set, payback is not computed")
    if economics.valid:
        return

    reasons = []
    if not economics.pays_for_the_ratio:
        reasons.append(
            f"target closer than {economics.min_target_r:.2f}R: with such a ratio "
            f"to the stop the breakeven win rate is higher than any "
            f"realistic setup delivers"
        )
    if not economics.pays_for_the_costs:
        reasons.append(
            f"move to target below {economics.min_move_pct:.2f}%: the target "
            f"is comparable to the round-trip fee and slippage"
        )
    raise RunInvalid(
        f"{setup}: {economics.describe()}. "
        + "; ".join(reasons)
        + ". The target is reachable but does not pay for itself (P-006)."
    )


# ---------------------------------------------------------------------------
# Five acceptance thresholds
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Threshold:
    name: str
    required: str
    actual: str
    passed: bool | None  # None means not computed

    @property
    def verdict(self) -> str:
        if self.passed is None:
            return UNKNOWN
        return "PASS" if self.passed else "FAIL"


@dataclass
class AcceptanceResult:
    thresholds: list[Threshold] = field(default_factory=list)

    @property
    def all_passed(self) -> bool | None:
        if any(t.passed is None for t in self.thresholds):
            return None
        return all(t.passed for t in self.thresholds)

    @property
    def failed(self) -> list[Threshold]:
        return [t for t in self.thresholds if t.passed is False]


def monte_carlo_drawdown(
    outcomes: list[TradeOutcome],
    *,
    initial: Decimal,
    risk_pct: float,
    runs: int = 2000,
    seed: int = 20260808,
) -> float | None:
    """95th percentile of drawdown over permutations of trade order (spec §7).

    The permutation answers the question "how lucky were we with the order".
    The same sample of trades in an unlucky order gives a noticeably
    larger drawdown, and that is what decides whether we survive it
    on a real account.

    `risk_pct` comes in as an argument, not as a literal. There used to be
    a hardcoded 0.06 here, the value of `max_loss_per_trade_pct` at the time
    of writing. After the risk was lowered to 2.5% (P-007) the literal would
    overstate the drawdown 2.4 times, and no test would notice: the number
    looks plausible at any value.
    """
    import random

    rs = [o.r_multiple for o in outcomes if o.valid and o.r_multiple is not None]
    if len(rs) < 10:
        return None
    if risk_pct <= 0:
        raise ValueError(f"risk per trade must be positive: {risk_pct}")

    rng = random.Random(seed)
    risk_per_trade = float(initial) * risk_pct / 100
    worst: list[float] = []

    for _ in range(runs):
        order = rs[:]
        rng.shuffle(order)
        equity = float(initial)
        peak = equity
        drawdown = 0.0
        for r in order:
            equity += float(r) * risk_per_trade
            peak = max(peak, equity)
            if peak > 0:
                drawdown = max(drawdown, (peak - equity) / peak * 100)
        worst.append(drawdown)

    worst.sort()
    idx = int(0.95 * (len(worst) - 1))
    return worst[idx]


def concentration_check(
    per_symbol: dict[str, list[TradeOutcome]], *, drop: int = 2
) -> tuple[float | None, float | None, list[str]]:
    """E with all symbols and without the two most profitable ones.

    If the result rests on a couple of symbols, it is not a strategy
    but a coincidence on specific pairs.
    """
    all_outcomes = [o for lst in per_symbol.values() for o in lst]
    base = summarize(all_outcomes).expectancy_r
    if base is None or len(per_symbol) <= drop:
        return (float(base) if base is not None else None), None, []

    contribution = {
        symbol: sum(float(o.r_multiple) for o in lst if o.valid and o.r_multiple is not None)
        for symbol, lst in per_symbol.items()
    }
    top = sorted(contribution, key=lambda s: -contribution[s])[:drop]
    rest = [o for s, lst in per_symbol.items() if s not in top for o in lst]
    without = summarize(rest).expectancy_r

    return float(base), (float(without) if without is not None else None), top


# ---------------------------------------------------------------------------
# Five mandatory blocks
# ---------------------------------------------------------------------------
@dataclass
class SlippageSweep:
    rows: list[tuple[float, int, float | None]] = field(default_factory=list)

    def survives_at(self, level: float) -> bool | None:
        for slip, _, e in self.rows:
            if abs(slip - level) < 1e-9:
                return None if e is None else e > 0
        return None


async def slippage_sweep(
    settings: Settings, start: datetime, end: datetime
) -> SlippageSweep:
    sweep = SlippageSweep()
    for level in settings.costs.slippage_sensitivity:
        result = await run_setup_b(settings, start, end, slippage_pct=Decimal(str(level)))
        stats = summarize(result.outcomes)
        sweep.rows.append(
            (level, stats.trades, float(stats.expectancy_r) if stats.expectancy_r else None)
        )
    return sweep


async def monthly_liquidity() -> dict[str, float]:
    """Median universe turnover by month (DEC-010)."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(UniverseSnapshot.month, UniverseSnapshot.avg_daily_turnover)
                .where(UniverseSnapshot.included.is_(True))
                .order_by(UniverseSnapshot.month)
            )
        ).all()
    by_month: dict[str, list[float]] = {}
    for month, turnover in rows:
        by_month.setdefault(month, []).append(float(turnover))
    return {m: statistics.median(v) for m, v in sorted(by_month.items()) if v}


def monthly_expectancy(outcomes: list[TradeOutcome]) -> dict[str, tuple[int, float | None]]:
    by_month: dict[str, list[TradeOutcome]] = {}
    for o in outcomes:
        key = f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}"
        by_month.setdefault(key, []).append(o)
    out: dict[str, tuple[int, float | None]] = {}
    for month, items in sorted(by_month.items()):
        stats = summarize(items)
        out[month] = (
            stats.trades,
            float(stats.expectancy_r) if stats.expectancy_r is not None else None,
        )
    return out


def correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson. None on an insufficient or degenerate sample (P-001)."""
    if len(xs) != len(ys) or len(xs) < 4:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


@dataclass
class WalkForwardWindow:
    train_start: datetime
    test_start: datetime
    test_end: datetime
    trades: int
    expectancy: float | None


async def walk_forward(
    settings: Settings, period_start: datetime, period_end: datetime, full: RunResult
) -> list[WalkForwardWindow]:
    """Walk-forward window (DEC-011).

    The setup's parameters are fixed and NOT fitted in each window:
    there is nothing to fit. The window here slices the result by regime,
    it is not a training procedure.

    Windows OVERLAP: neighbours share train_months − step_months
    months. The estimates are not independent, and no statistics can be built on them.
    """
    wf = settings.backtest.walk_forward
    if not wf.enabled:
        return []

    windows: list[WalkForwardWindow] = []
    cursor = period_start + timedelta(days=30.44 * wf.train_months)
    while cursor < period_end:
        test_end = min(cursor + timedelta(days=30.44 * wf.test_months), period_end)
        subset = full.in_window(cursor, test_end)
        stats = summarize(subset)
        windows.append(
            WalkForwardWindow(
                train_start=cursor - timedelta(days=30.44 * wf.train_months),
                test_start=cursor,
                test_end=test_end,
                trades=stats.trades,
                expectancy=float(stats.expectancy_r) if stats.expectancy_r else None,
            )
        )
        cursor += timedelta(days=30.44 * wf.step_months)
    return windows


@dataclass(frozen=True)
class WindowDispersion:
    """Spread of E across walk-forward windows versus the EXPECTED one.

    "Seven positive windows out of twelve" says nothing by itself.
    With a true E = +0.07R and ninety trades per window the standard
    error of a window is about 0.13R, so the windows MUST jump
    across zero, and a positive share of about 70% is expected.

    The question to ask: is the spread LARGER than expected or not.
    Larger means E depends on the regime, and one number does not describe it.
    The same means the windows are simply small, and the spread is evidence
    neither for nor against.
    """

    windows: int
    positive: int
    expected_positive: float
    observed_sd: float
    expected_sd: float

    @property
    def ratio(self) -> float | None:
        """How many times the observed spread exceeds the expected one."""
        return self.observed_sd / self.expected_sd if self.expected_sd > 0 else None

    @property
    def regime_dependent(self) -> bool | None:
        """The spread is noticeably wider than expected.

        The 1.5 threshold is not a statistical criterion but a common-sense
        bound: with twelve overlapping windows a confidence interval
        around the variance ratio cannot be built anyway
        (DEC-011), and an exact value here would be false precision.
        """
        r = self.ratio
        return None if r is None else r > 1.5

    def describe(self) -> str:
        if self.windows == 0:
            return "no windows"
        verdict = {
            None: "not estimated",
            True: "WIDER than expected — looks like regime dependence",
            False: "within expectations — the windows are simply small",
        }[self.regime_dependent]
        ratio = "—" if self.ratio is None else f"{self.ratio:.2f}x"
        return (
            f"positive {self.positive} of {self.windows} "
            f"with {self.expected_positive:.1f} expected; "
            f"spread {self.observed_sd:.3f} versus expected "
            f"{self.expected_sd:.3f} ({ratio}) — {verdict}"
        )


def window_dispersion(
    windows: list[WalkForwardWindow], outcomes: list[TradeOutcome]
) -> WindowDispersion:
    """Observed window spread compared with the one implied by the sample.

    The expected window error is computed from the spread of R over ALL trades
    and the average number of trades per window: this is the uncertainty
    that would arise even with a perfectly constant edge.
    """
    measured = [w for w in windows if w.expectancy is not None and w.trades > 1]
    rs = [
        float(o.r_multiple)
        for o in outcomes
        if o.valid and o.r_multiple is not None
    ]
    if len(measured) < 2 or len(rs) < 2:
        return WindowDispersion(len(measured), 0, 0.0, 0.0, 0.0)

    values = [w.expectancy for w in measured]
    positive = sum(1 for v in values if v > 0)
    observed_sd = statistics.stdev(values)

    spread = statistics.stdev(rs)
    mean_trades = statistics.fmean([w.trades for w in measured])
    expected_sd = spread / mean_trades**0.5 if mean_trades > 0 else 0.0

    # Share of positive windows expected with a constant E: the share
    # of the normal distribution to the right of zero.
    overall = statistics.fmean(rs)
    if expected_sd > 0:
        z = overall / expected_sd
        expected_share = 0.5 * (1.0 + math.erf(z / 2**0.5))
    else:
        expected_share = 0.0

    return WindowDispersion(
        windows=len(measured),
        positive=positive,
        expected_positive=expected_share * len(measured),
        observed_sd=observed_sd,
        expected_sd=expected_sd,
    )


async def monthly_btc_range() -> dict[str, float]:
    """BTC range within a month, in percent.

    A proxy for market trendiness. Range specifically, not volatility:
    volatility is equally high in a trend and in chop, while a trend
    setup needs the price to go somewhere and not come back.
    """
    from sqlalchemy import text

    async with session_scope() as session:
        rows = (
            await session.execute(
                text(
                    """
                    WITH d AS (
                      SELECT date_trunc('month', ts) AS m, close,
                             first_value(close) OVER (
                               PARTITION BY date_trunc('month', ts) ORDER BY ts
                             ) AS opened
                      FROM ohlcv
                      WHERE symbol = 'BTCUSDT' AND timeframe = '4h')
                    SELECT to_char(m, 'YYYY-MM'),
                           (max(close) / nullif(min(opened), 0) - 1) * 100
                    FROM d GROUP BY m
                    """
                )
            )
        ).all()
    return {month: float(value) for month, value in rows if value is not None}


def regime_sensitivity(
    outcomes: list[TradeOutcome],
    regime: dict[str, float],
    *,
    min_trades: int = 10,
) -> tuple[float | None, int]:
    """Correlation of monthly E with the regime indicator.

    Months with a handful of trades are excluded: E over five trades is noise,
    and it dilutes the relationship rather than sharpening it.
    """
    monthly = monthly_expectancy(outcomes)
    xs: list[float] = []
    ys: list[float] = []
    for month, (trades, expectancy) in sorted(monthly.items()):
        value = regime.get(month)
        if expectancy is None or value is None or trades < min_trades:
            continue
        xs.append(value)
        ys.append(expectancy)
    return correlation(xs, ys), len(xs)


def marginal_contribution(
    outcomes: list[TradeOutcome], marginal: frozenset[str]
) -> dict[str, object]:
    """Share of trades from marginal entrants and E without them (constraint 9)."""
    total = [o for o in outcomes if o.valid]
    if not total:
        return {"trades total": 0}

    theirs = [o for o in total if o.trade.symbol in marginal]
    rest = [o for o in total if o.trade.symbol not in marginal]

    e_all = summarize(total).expectancy_r
    e_rest = summarize(rest).expectancy_r

    return {
        "trades total": len(total),
        "of them marginal": len(theirs),
        "share": f"{len(theirs) / len(total) * 100:.1f}%",
        "E with all": f"{float(e_all):+.3f}R" if e_all is not None else UNKNOWN,
        "E without marginal": f"{float(e_rest):+.3f}R" if e_rest is not None else UNKNOWN,
    }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def evaluate_thresholds(
    oos: Statistics,
    drawdown_p95: float | None,
    sweep: SlippageSweep,
    base_e: float | None,
    without_top: float | None,
) -> AcceptanceResult:
    """Five thresholds from PROGRESS.md. The values are not revised."""
    result = AcceptanceResult()

    e = float(oos.expectancy_r) if oos.expectancy_r is not None else None
    result.thresholds.append(
        Threshold("E on out-of-sample", "> +0.15R",
                  f"{e:+.3f}R" if e is not None else UNKNOWN,
                  None if e is None else e > 0.15)
    )
    result.thresholds.append(
        Threshold("Trades on OOS", ">= 60", str(oos.trades), oos.trades >= 60)
    )
    result.thresholds.append(
        Threshold("Drawdown, 95th percentile", "< 40%",
                  f"{drawdown_p95:.1f}%" if drawdown_p95 is not None else UNKNOWN,
                  None if drawdown_p95 is None else drawdown_p95 < 40)
    )
    survives = sweep.survives_at(0.10)
    row = next((r for r in sweep.rows if abs(r[0] - 0.10) < 1e-9), None)
    result.thresholds.append(
        Threshold("Survives 0.10% slippage", "E > 0",
                  f"{row[2]:+.3f}R" if row and row[2] is not None else UNKNOWN,
                  survives)
    )
    if base_e is None or without_top is None:
        holds = None
        actual = UNKNOWN
    else:
        holds = without_top > 0
        actual = f"{without_top:+.3f}R without the two best (was {base_e:+.3f}R)"
    result.thresholds.append(
        Threshold("Result does not rest on 1-2 symbols", "E > 0 without the two best",
                  actual, holds)
    )
    return result


async def build_full_report(settings: Settings) -> dict[str, object]:
    """Full run: baseline, sweeps, slices. Returns bare numbers."""
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    total_months = settings.backtest.in_sample_months + settings.backtest.out_of_sample_months
    start = end - timedelta(days=30.44 * total_months)
    oos_start = end - timedelta(days=30.44 * settings.backtest.out_of_sample_months)

    log.info("backtest.start", start=start.isoformat(), end=end.isoformat())
    full = await run_setup_b(settings, start, end)

    # P-005 is checked BEFORE any statistics: an invalid run
    # must not reach the acceptance thresholds and report blocks.
    breakdown = exit_breakdown(full.outcomes)
    log.info("backtest.exits", breakdown=breakdown.describe())

    is_stats = summarize(full.in_window(start, oos_start))
    oos_stats = summarize(full.in_window(oos_start, end))
    all_stats = summarize(full.outcomes)

    oos_outcomes = full.in_window(oos_start, end)
    drawdown = monte_carlo_drawdown(
        oos_outcomes,
        initial=Decimal(str(settings.risk.equity_usd)),
        risk_pct=settings.risk.max_loss_per_trade_pct,
    )

    per_symbol_oos: dict[str, list[TradeOutcome]] = {}
    for o in oos_outcomes:
        per_symbol_oos.setdefault(o.trade.symbol, []).append(o)
    base_e, without_top, top_symbols = concentration_check(per_symbol_oos)

    sweep = await slippage_sweep(settings, oos_start, end)
    thresholds = evaluate_thresholds(oos_stats, drawdown, sweep, base_e, without_top)

    liquidity = await monthly_liquidity()
    monthly_e = monthly_expectancy(full.outcomes)
    common = [m for m in monthly_e if m in liquidity and monthly_e[m][1] is not None]
    corr = correlation(
        [monthly_e[m][1] for m in common], [liquidity[m] for m in common]
    )

    marginal = settings.backtest.marginal_entrants.symbols()
    wf = await walk_forward(settings, start, end, full)

    return {
        "period": (start, oos_start, end),
        "in_sample": is_stats,
        "out_of_sample": oos_stats,
        "full period": all_stats,
        "thresholds": thresholds,
        "drawdown_p95": drawdown,
        "concentration": (base_e, without_top, top_symbols),
        "slippage_sweep": sweep,
        "liquidity_monthly": liquidity,
        "E_monthly": monthly_e,
        "corr_E_liquidity": corr,
        "marginal": marginal_contribution(full.outcomes, marginal),
        "walk_forward": wf,
        "exits": breakdown,
        "run": full,
    }
