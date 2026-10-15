"""Setup evaluation on a window: everything needed for a verdict, in one piece of code.

Until now the battery of checks lived inside CLI commands, one copy per
command. For validating two setups that would mean two copies of the
verdict computation, and a divergence between them would surface when
one setup passed where the other would have failed on the same
number.
"""

from __future__ import annotations

import functools
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from src.backtest.accounting import Statistics, summarize
from src.backtest.portfolio import PortfolioResult, replay
from src.backtest.report import (
    AcceptanceResult,
    ExitBreakdown,
    SlippageSweep,
    concentration_check,
    evaluate_thresholds,
    exit_breakdown,
    monte_carlo_drawdown,
)
from src.backtest.runner import (
    RunResult,
    load_instrument_params,
    run_setup_c,
    run_setup_d,
)
from src.core.config import Settings
from src.risk.correlation import CorrelationSource
from src.risk.governor import GovernorLimits

#: Null hypothesis seeds. Several, because the question is about the spread,
#: and pooling their trades gives an honest error (DEC-021).
NULL_SEEDS: tuple[int, ...] = (1, 2, 3, 4, 5)

#: Seed for the order of concurrent candidates in the portfolio (DEC-033).
TIE_BREAK_SEED = 20260924


@dataclass
class NullComparison:
    e_setup: float
    e_null: float
    z: float
    null_trades: int


@dataclass
class Evaluation:
    setup: str
    start: datetime
    end: datetime
    result: RunResult
    stats: Statistics
    breakdown: ExitBreakdown
    sweep: SlippageSweep
    base_e: float | None
    without_top: float | None
    top: list[str]
    book: PortfolioResult
    dd_portfolio: float | None
    dd_population: float | None
    acceptance: AcceptanceResult
    null: NullComparison | None = None
    #: Median absolute trade return, %: the guard of the time exit.
    median_move_pct: float | None = None
    se: float | None = None
    notes: list[str] = field(default_factory=list)


#: Setups with a time-series momentum rule: D and E (DEC-038).
MOMENTUM_SETUPS = frozenset({"setup_d", "setup_e"})

#: Directional-bet setups: the null hypothesis is direction by
#: coin flip, concurrent candidates are shuffled (DEC-033, DEC-042).
DIRECTIONAL_SETUPS = MOMENTUM_SETUPS | {"setup_f", "setup_g", "setup_h"}


def _runner(setup: str):  # noqa: ANN202
    if setup == "setup_c":
        return run_setup_c
    if setup in MOMENTUM_SETUPS:
        return functools.partial(run_setup_d, stream_setup=setup)
    if setup == "setup_f":
        from src.backtest.runner_f import run_setup_f

        return run_setup_f
    if setup == "setup_g":
        from src.backtest.runner_g import run_setup_g

        return run_setup_g
    if setup == "setup_h":
        from src.backtest.runner_h import run_setup_h

        return run_setup_h
    raise ValueError(f"evaluation is not supported for {setup}")


def _kwargs(setup: str, settings: Settings) -> dict:
    """Run parameters that differ between setups.

    Setup C gets breakeven passed explicitly (DEC-020). Setup D does
    not: it contradicts the hypothesis "hold for a week" (DEC-033).
    """
    if setup == "setup_c":
        return {
            "breakeven_at_r": Decimal(
                str(settings.strategy["management"]["breakeven_at_r"])
            )
        }
    return {}


def _median_move_pct(result: RunResult) -> float | None:
    moves = [
        abs(float(o.trade.exit_price / o.trade.entry) - 1) * 100
        for o in result.outcomes
        if o.valid and o.trade.exit_price is not None and o.trade.entry > 0
    ]
    return statistics.median(moves) if moves else None


async def evaluate(
    settings: Settings,
    setup: str,
    start: datetime,
    end: datetime,
    *,
    drawdown_basis: str = "portfolio",
    with_null: bool = False,
) -> Evaluation:
    run = _runner(setup)
    extra = _kwargs(setup, settings)

    result = await run(settings, start, end, **extra)
    stats = summarize(result.outcomes)
    breakdown = exit_breakdown(result.outcomes)

    sweep = SlippageSweep()
    for level in settings.costs.slippage_sensitivity:
        at = await run(settings, start, end, slippage_pct=Decimal(str(level)), **extra)
        s = summarize(at.outcomes)
        sweep.rows.append(
            (level, s.trades, float(s.expectancy_r) if s.expectancy_r is not None else None)
        )

    base_e, without, top = concentration_check(result.per_symbol)

    source = CorrelationSource(settings.risk.correlation_lookback_days)
    instruments = await load_instrument_params(sorted(result.per_symbol))
    book = await replay(
        result.per_symbol,
        limits=GovernorLimits.from_config(settings.risk),
        instruments=instruments,
        initial_equity=Decimal(str(settings.risk.equity_usd)),
        margin_pct=Decimal(str(settings.risk.margin_per_trade_pct)),
        max_loss_pct=Decimal(str(settings.risk.max_loss_per_trade_pct)),
        leverage_cap=Decimal(str(settings.risk.leverage_cap)),
        min_liq_buffer_ratio=Decimal(str(settings.risk.min_liq_buffer_ratio)),
        correlation=source.between,
        # Setup C: order by name, as its numbers were computed;
        # Setup D: shuffling (DEC-033).
        tie_break_seed=TIE_BREAK_SEED if setup in DIRECTIONAL_SETUPS else None,
        max_position_pct=(
            Decimal(str(settings.risk.max_position_pct))
            if settings.risk.max_position_pct is not None
            else None
        ),
    )
    risk_pct = settings.risk.max_loss_per_trade_pct
    initial = Decimal(str(settings.risk.equity_usd))
    dd_portfolio = monte_carlo_drawdown(book.taken, initial=initial, risk_pct=risk_pct)
    dd_population = monte_carlo_drawdown(result.outcomes, initial=initial, risk_pct=risk_pct)
    drawdown = dd_portfolio if drawdown_basis == "portfolio" else dd_population

    acceptance = evaluate_thresholds(stats, drawdown, sweep, base_e, without)

    rs = [float(o.r_multiple) for o in result.outcomes if o.valid]
    se = statistics.stdev(rs) / len(rs) ** 0.5 if len(rs) > 1 else None

    evaluation = Evaluation(
        setup=setup, start=start, end=end, result=result, stats=stats,
        breakdown=breakdown, sweep=sweep, base_e=base_e, without_top=without,
        top=top, book=book, dd_portfolio=dd_portfolio, dd_population=dd_population,
        acceptance=acceptance, median_move_pct=_median_move_pct(result), se=se,
    )

    if with_null:
        if setup not in DIRECTIONAL_SETUPS:
            raise ValueError("the null hypothesis \"direction by coin flip\" is defined for D–H")
        from src.backtest.benchmark import advantage_over

        noise: list[float] = []
        for seed in NULL_SEEDS:
            flipped = await run(settings, start, end, coin_flip_seed=seed, **extra)
            noise.extend(float(o.r_multiple) for o in flipped.outcomes if o.valid)
        verdict = advantage_over(rs, noise)
        if verdict is not None:
            e_setup, e_null, z = verdict
            evaluation.null = NullComparison(e_setup, e_null, z, len(noise))
        else:
            evaluation.notes.append("nothing to compare the null hypothesis with")

    return evaluation


__all__ = [
    "DIRECTIONAL_SETUPS",
    "NULL_SEEDS",
    "TIE_BREAK_SEED",
    "Evaluation",
    "NullComparison",
    "evaluate",
]
