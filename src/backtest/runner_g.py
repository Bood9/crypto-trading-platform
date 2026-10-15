"""Setup G run. DEC-044.

A separate file for the same reason as `runner_f.py`: G's freeze
must not depend on edits to the shared `runner.py`.

The difference from the D–F runs is two passes. The rank needs the returns
of ALL symbols at every close, so first the daily bars of all candidates
are read and a `RankBook` is built, and only then does each symbol go through
the same engine, sizer and universe selection as E.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest.engine import BacktestConfig, MarketBar, run
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
from src.strategy.setup_a import build_atr_series
from src.strategy.setup_g import RankBook, SetupGParams, make_signal_fn, warmup_bars


def month_key(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


async def sweep_setup_g(
    settings: Settings,
    start: datetime,
    end: datetime,
    variants: list[tuple[str, SetupGParams]],
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
    available = await stream_months("setup_g", settings)
    names = symbols if symbols is not None else sorted(membership)
    results = {
        label: RunResult(setup="setup_g", symbols_considered=len(names)) for label, _ in variants
    }
    instruments = await load_instrument_params(names)
    slip = slippage_pct if slippage_pct is not None else Decimal(str(settings.costs.slippage_pct))
    max_warmup = max(warmup_bars(v) for _, v in variants)
    padded = start - DAY * (max_warmup + 5)

    # Pass 1: daily bars of all candidates and the months they are admitted in.
    months: dict[str, set[str]] = {}
    daily: dict[str, list[MarketBar]] = {}
    for symbol in names:
        allowed = membership.get(symbol, set()) & available.get(symbol, set())
        if not allowed:
            continue
        bars = to_daily(await load_bars(symbol, "4h", padded, end), timedelta(hours=4))
        if len(bars) < max_warmup + 1:
            continue
        months[symbol] = allowed
        daily[symbol] = bars

    def member(symbol: str, bar_ts: datetime) -> bool:
        """Universe of the bar's month, point-in-time, as for the other setups."""
        return month_key(bar_ts) in months.get(symbol, set())

    books = {label: RankBook(daily, params, member) for label, params in variants}

    # Pass 2: each symbol goes through the engine, as for E.
    for symbol, bars in daily.items():
        spec = instruments.get(symbol)
        if spec is None:
            for result in results.values():
                result.rejections["MISSING_INSTRUMENT"] = (
                    result.rejections.get("MISSING_INSTRUMENT", 0) + 1
                )
            continue
        atr_series = build_atr_series(bars, reference.stop_atr_period, DAY)
        if not atr_series.values:
            continue
        schedule = await load_funding_schedule(symbol, padded, end)
        mmr, qty_step, min_qty, min_notional = spec

        for label, params in variants:
            result = results[label]
            cfg = BacktestConfig(
                initial_equity=Decimal(str(settings.risk.equity_usd)),
                taker_fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
                slippage_pct=slip,
                time_stop=timedelta(hours=params.time_stop_hours),
                warmup_bars=warmup_bars(params),
            )
            seed = f"{coin_flip_seed}:{symbol}" if coin_flip_seed is not None else None
            signal_fn = make_signal_fn(
                params, symbol, atr_series, books[label], funnel, coin_flip_seed=seed
            )
            rejections: dict[str, int] = {}
            sizer = make_sizer(
                settings, mmr=mmr, qty_step=qty_step, min_qty=min_qty,
                min_notional=min_notional, equity=Decimal(str(settings.risk.equity_usd)),
                rejections=rejections,
            )
            symbol_result = run(bars, signal_fn, sizer, cfg, symbol=symbol, funding=schedule)
            opened = [o for o in symbol_result.outcomes if o.trade.opened_at >= start]
            kept = [o for o in opened if month_key(o.trade.opened_at) in months[symbol]]
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


async def run_setup_g(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    params: SetupGParams | None = None,
    funnel: Funnel | None = None,
    coin_flip_seed: int | None = None,
    breakeven_at_r: Decimal | None = None,
) -> RunResult:
    """`breakeven_at_r` is for the shared runner signature; ignored (DEC-044)."""
    del breakeven_at_r
    params = params or SetupGParams.from_config(settings.strategy["setup_g"])
    results = await sweep_setup_g(
        settings, start, end, [("baseline", params)],
        slippage_pct=slippage_pct, symbols=symbols, funnel=funnel, coin_flip_seed=coin_flip_seed,
    )
    return results["baseline"]


__all__ = ["month_key", "run_setup_g", "sweep_setup_g"]
