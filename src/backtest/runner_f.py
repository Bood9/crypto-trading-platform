"""Setup F run. DEC-042.

A separate file, not a function in `runner.py`: `runner.py` is part of the
shared frozen code, and the next setup added there would shift
F's freeze, so its test would be rejected for drift in code that
F did not touch. The symbol loop repeats `sweep_setup_d` deliberately:
the same daily-bar build, the same universe and stream selection, the same
sizer, so that F differs from E only by the signal rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.backtest.engine import BacktestConfig, run
from src.backtest.funnel import Funnel
from src.backtest.resample import DAY, to_daily
from src.backtest.runner import (
    RunResult,
    load_bars,
    load_funding_schedule,
    load_instrument_params,
    make_sizer,
    stream_months,
    universe_membership,
)
from src.core.config import Settings
from src.core.db import FundingHistory, session_scope
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_f import (
    FundingTape,
    PressureSeries,
    SetupFParams,
    make_signal_fn,
    warmup_bars,
)


async def load_funding_tape(symbol: str, start: datetime, end: datetime) -> FundingTape:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(FundingHistory.ts, FundingHistory.rate, FundingHistory.interval_minutes)
                .where(
                    FundingHistory.symbol == symbol,
                    FundingHistory.ts >= start,
                    FundingHistory.ts <= end,
                )
                .order_by(FundingHistory.ts)
            )
        ).all()
    return FundingTape(
        ts=[r[0] for r in rows], rates=[r[1] for r in rows], interval_minutes=[r[2] for r in rows]
    )


async def sweep_setup_f(
    settings: Settings,
    start: datetime,
    end: datetime,
    variants: list[tuple[str, SetupFParams]],
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    funnel: Funnel | None = None,
    coin_flip_seed: int | None = None,
) -> dict[str, RunResult]:
    if not variants:
        raise ValueError("sweep without variants")
    if len({v.stop_atr_period for _, v in variants}) > 1:
        raise ValueError("variants with different ATR periods read different series")
    if funnel is not None and len(variants) > 1:
        raise ValueError("the funnel is computed for a single variant, not for a sweep")

    reference = variants[0][1]
    membership = await universe_membership()
    available = await stream_months("setup_f", settings)
    names = symbols if symbols is not None else sorted(membership)
    results = {
        label: RunResult(setup="setup_f", symbols_considered=len(names)) for label, _ in variants
    }
    instruments = await load_instrument_params(names)
    slip = slippage_pct if slippage_pct is not None else Decimal(str(settings.costs.slippage_pct))
    # Percentile history and the measure window: back from the period start.
    lookback = max(v.history_days + v.funding_window_days for _, v in variants)
    max_warmup = max(warmup_bars(v) for _, v in variants)

    for symbol in names:
        months = membership.get(symbol, set()) & available.get(symbol, set())
        if not months:
            continue
        spec = instruments.get(symbol)
        if spec is None:
            for result in results.values():
                result.rejections["MISSING_INSTRUMENT"] = (
                    result.rejections.get("MISSING_INSTRUMENT", 0) + 1
                )
            continue

        padded = start - DAY * (lookback + max_warmup + 5)
        bars = to_daily(await load_bars(symbol, "4h", padded, end), timedelta(hours=4))
        if len(bars) < max_warmup + 1:
            continue
        atr_series = build_atr_series(bars, reference.stop_atr_period, DAY)
        if not atr_series.values:
            continue
        tape = await load_funding_tape(symbol, padded - DAY * 8, end)
        schedule = await load_funding_schedule(symbol, padded, end)
        closes = [b.ts + DAY for b in bars]
        mmr, qty_step, min_qty, min_notional = spec

        for label, params in variants:
            result = results[label]
            pressure = PressureSeries(tape, closes, params)
            cfg = BacktestConfig(
                initial_equity=Decimal(str(settings.risk.equity_usd)),
                taker_fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
                slippage_pct=slip,
                time_stop=timedelta(hours=params.time_stop_hours),
                warmup_bars=warmup_bars(params),
            )
            seed = f"{coin_flip_seed}:{symbol}" if coin_flip_seed is not None else None
            signal_fn = make_signal_fn(params, atr_series, pressure, funnel, coin_flip_seed=seed)
            rejections: dict[str, int] = {}
            sizer = make_sizer(
                settings, mmr=mmr, qty_step=qty_step, min_qty=min_qty,
                min_notional=min_notional, equity=Decimal(str(settings.risk.equity_usd)),
                rejections=rejections,
            )
            symbol_result = run(bars, signal_fn, sizer, cfg, symbol=symbol, funding=schedule)
            opened = [o for o in symbol_result.outcomes if o.trade.opened_at >= start]
            kept = [
                o for o in opened
                if f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}" in months
            ]
            if kept:
                result.per_symbol[symbol] = kept
                result.outcomes.extend(kept)
                result.symbols_traded += 1
            if funnel is not None:
                for _ in opened:
                    funnel.enter("funded")
                for _ in kept:
                    funnel.enter("in_universe")
            for code, n in rejections.items():
                result.rejections[code] = result.rejections.get(code, 0) + n

    return results


async def run_setup_f(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    params: SetupFParams | None = None,
    funnel: Funnel | None = None,
    coin_flip_seed: int | None = None,
    breakeven_at_r: Decimal | None = None,
) -> RunResult:
    """`breakeven_at_r` is for the shared runner signature; ignored (DEC-042)."""
    del breakeven_at_r
    params = params or SetupFParams.from_config(settings.strategy["setup_f"])
    results = await sweep_setup_f(
        settings, start, end, [("baseline", params)],
        slippage_pct=slippage_pct, symbols=symbols, funnel=funnel, coin_flip_seed=coin_flip_seed,
    )
    return results["baseline"]


__all__ = ["load_funding_tape", "run_setup_f", "sweep_setup_f"]
