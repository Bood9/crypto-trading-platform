"""A setup run over the universe respecting the point-in-time composition.

A symbol trades in month M only if it is in the universe snapshot
for that month. Otherwise the backtest would use today's knowledge of
which pairs turn out liquid: the very survivorship bias that
DEC-003 removes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from src.backtest.accounting import TradeOutcome, summarize
from src.backtest.engine import BacktestConfig, FundingSchedule, MarketBar, run
from src.backtest.funnel import Funnel
from src.core.config import Settings
from src.core.db import (
    FundingHistory,
    Ohlcv,
    OpenInterest,
    StreamAvailability,
    UniverseSnapshot,
    session_scope,
)
from src.core.logging import get_logger
from src.core.models import RejectionCode
from src.risk.position_sizer import SizingInput, calculate_size
from src.strategy.setup_a import SetupAParams, build_atr_series
from src.strategy.setup_a import make_signal_fn as make_signal_fn_a
from src.strategy.setup_a import warmup_bars as warmup_bars_a
from src.strategy.setup_b import SetupBParams, SymbolContext, make_signal_fn
from src.strategy.setup_c import SetupCParams
from src.strategy.setup_c import make_signal_fn as make_signal_fn_c
from src.strategy.setup_c import warmup_bars as warmup_bars_c
from src.strategy.setup_d import SetupDParams
from src.strategy.setup_d import make_signal_fn as make_signal_fn_d
from src.strategy.setup_d import warmup_bars as warmup_bars_d

log = get_logger(__name__)


@dataclass
class SymbolRun:
    symbol: str
    outcomes: list[TradeOutcome] = field(default_factory=list)
    bars: int = 0
    rejections: dict[str, int] = field(default_factory=dict)


@dataclass
class RunResult:
    """Result of a run over the whole universe."""

    setup: str
    outcomes: list[TradeOutcome] = field(default_factory=list)
    per_symbol: dict[str, list[TradeOutcome]] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    symbols_traded: int = 0
    symbols_considered: int = 0

    def in_window(self, start: datetime, end: datetime) -> list[TradeOutcome]:
        """Trades OPENED inside the window.

        By opening, not by closing: a trade belongs to the period
        in which the decision was made.
        """
        return [o for o in self.outcomes if start <= o.trade.opened_at < end]


async def load_bars(symbol: str, timeframe: str, start: datetime, end: datetime) -> list[MarketBar]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(
                    Ohlcv.ts, Ohlcv.open, Ohlcv.high, Ohlcv.low, Ohlcv.close, Ohlcv.volume
                )
                .where(
                    Ohlcv.symbol == symbol,
                    Ohlcv.timeframe == timeframe,
                    Ohlcv.ts >= start,
                    Ohlcv.ts < end,
                )
                .order_by(Ohlcv.ts)
            )
        ).all()
    return [MarketBar(ts, o, h, low, c, v) for ts, o, h, low, c, v in rows]


async def load_context(symbol: str, start: datetime, end: datetime) -> SymbolContext:
    """OI and funding with a margin back for the percentile window."""
    padded = start - timedelta(days=40)
    async with session_scope() as s:
        oi = (
            await s.execute(
                select(OpenInterest.ts, OpenInterest.open_interest)
                .where(
                    OpenInterest.symbol == symbol,
                    OpenInterest.interval == "1h",
                    OpenInterest.ts >= padded,
                    OpenInterest.ts < end,
                )
                .order_by(OpenInterest.ts)
            )
        ).all()
        funding = (
            await s.execute(
                select(FundingHistory.ts, FundingHistory.rate_8h)
                .where(
                    FundingHistory.symbol == symbol,
                    FundingHistory.ts >= padded,
                    FundingHistory.ts < end,
                )
                .order_by(FundingHistory.ts)
            )
        ).all()
    return SymbolContext(
        oi_ts=[r[0] for r in oi],
        oi_values=[r[1] for r in oi],
        funding_ts=[r[0] for r in funding],
        funding_rates=[r[1] for r in funding],
    )


async def load_funding_schedule(
    symbol: str, start: datetime, end: datetime
) -> FundingSchedule:
    """Raw funding rates for a symbol.

    Raw precisely: normalizing to eight hours is needed to compare
    symbols with each other, while for counting what was charged it is
    unnecessary and moreover is computed by TODAY's interval, which changed
    for some symbols (DEC-025).
    """
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(FundingHistory.ts, FundingHistory.rate)
                .where(
                    FundingHistory.symbol == symbol,
                    FundingHistory.ts >= start,
                    FundingHistory.ts <= end,
                )
                .order_by(FundingHistory.ts)
            )
        ).all()
    return FundingSchedule(
        ts=[r[0] for r in rows], rates=[r[1] for r in rows]
    )


async def universe_membership() -> dict[str, set[str]]:
    """Months in which each symbol was in the universe."""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(UniverseSnapshot.symbol, UniverseSnapshot.month).where(
                    UniverseSnapshot.included.is_(True)
                )
            )
        ).all()
    out: dict[str, set[str]] = {}
    for symbol, month in rows:
        out.setdefault(symbol, set()).add(month)
    return out


def make_sizer(settings: Settings, mmr: Decimal, qty_step: Decimal, min_qty: Decimal,
               min_notional: Decimal, equity: Decimal, rejections: dict[str, int]):
    """Position size via the risk manager. Refusals are counted by code."""
    risk = settings.risk

    def sizer(signal, price):
        result = calculate_size(
            SizingInput(
                equity=equity,
                entry_price=signal.entry,
                stop_price=signal.stop,
                mmr=mmr,
                qty_step=qty_step,
                min_order_qty=min_qty,
                min_notional=min_notional,
                margin_pct=Decimal(str(risk.margin_per_trade_pct)),
                max_loss_pct=Decimal(str(risk.max_loss_per_trade_pct)),
                leverage_cap=Decimal(str(risk.leverage_cap)),
                min_liq_buffer_ratio=Decimal(str(risk.min_liq_buffer_ratio)),
                max_position_pct=(
                    Decimal(str(risk.max_position_pct))
                    if risk.max_position_pct is not None
                    else None
                ),
            )
        )
        if not result.approved:
            code = result.rejection.value if result.rejection else RejectionCode.EXCHANGE_ERROR
            rejections[code] = rejections.get(code, 0) + 1
            return None
        return result.qty, result.notional, result.margin

    return sizer


async def run_setup_b(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
) -> RunResult:
    """Setup B run over the whole universe.

    Symbols are processed independently: the concurrent position limit
    is applied at the merge stage, not here, otherwise the symbol
    traversal order would affect the result.
    """
    params = SetupBParams.from_config(settings.strategy["setup_b"])
    membership = await universe_membership()
    names = symbols if symbols is not None else sorted(membership)

    result = RunResult(setup="setup_b", symbols_considered=len(names))
    instruments = await load_instrument_params(names)
    slip = slippage_pct if slippage_pct is not None else Decimal(str(settings.costs.slippage_pct))

    cfg = BacktestConfig(
        initial_equity=Decimal(str(settings.risk.equity_usd)),
        taker_fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
        slippage_pct=slip,
        time_stop=timedelta(hours=params.time_stop_hours),
        warmup_bars=max(params.breakout_lookback, params.stop_lookback_bars) + 1,
    )

    for symbol in names:
        bars = await load_bars(symbol, "4h", start, end)
        if len(bars) < cfg.warmup_bars + 1:
            continue

        months = membership.get(symbol, set())
        if not months:
            continue

        # Point-in-time: a bar is traded only if the symbol was
        # in the universe in THIS month.
        tradable = [b for b in bars if f"{b.ts.year:04d}-{b.ts.month:02d}" in months]
        if len(tradable) < cfg.warmup_bars + 1:
            continue

        context = await load_context(symbol, start, end)
        signal_fn = make_signal_fn(context, params)
        rejections: dict[str, int] = {}

        # Instrument parameters only from the API cache (spec §16).
        # A symbol may be missing from the cache if it was delisted; then
        # we take conservative values and flag it.
        spec = instruments.get(symbol)
        if spec is None:
            result.rejections["MISSING_INSTRUMENT"] = (
                result.rejections.get("MISSING_INSTRUMENT", 0) + 1
            )
            continue
        mmr, qty_step, min_qty, min_notional = spec

        sizer = make_sizer(
            settings,
            mmr=mmr,
            qty_step=qty_step,
            min_qty=min_qty,
            min_notional=min_notional,
            equity=Decimal(str(settings.risk.equity_usd)),
            rejections=rejections,
        )

        symbol_result = run(bars, signal_fn, sizer, cfg, symbol=symbol)
        kept = [
            o
            for o in symbol_result.outcomes
            if f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}" in months
        ]

        if kept:
            result.per_symbol[symbol] = kept
            result.outcomes.extend(kept)
            result.symbols_traded += 1
        for code, n in rejections.items():
            result.rejections[code] = result.rejections.get(code, 0) + n

    return result




async def stream_months(setup: str, settings: Settings) -> dict[str, set[str]]:
    """Months in which ALL streams the setup needs are available for the symbol.

    Intersection, not union: MULTIUSDT serves candles but not OI, and a
    setup reading OI on such a symbol computes something other than it thinks.
    Admitting a symbol as a whole is impossible here, because gaps can be
    monthly (DEC-012).
    """
    required = settings.data.stream_requirements.model_dump()[setup]
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(StreamAvailability.symbol, StreamAvailability.month)
                .where(
                    StreamAvailability.stream.in_(required),
                    StreamAvailability.available.is_(True),
                )
            )
        ).all()
    seen: dict[tuple[str, str], int] = {}
    for symbol, month in rows:
        seen[(symbol, month)] = seen.get((symbol, month), 0) + 1

    need = len(required)
    out: dict[str, set[str]] = {}
    for (symbol, month), count in seen.items():
        if count >= need:
            out.setdefault(symbol, set()).add(month)
    return out


async def sweep_setup_a(
    settings: Settings,
    start: datetime,
    end: datetime,
    variants: list[tuple[str, SetupAParams]],
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    funnel: Funnel | None = None,
) -> dict[str, RunResult]:
    """Several parameter sets in ONE pass over the data.

    A filter sweep is a dozen runs, and almost all their time goes into
    reading bars from the database, not into computing. A symbol is loaded
    once, and the variants run over the series already read: twelve
    runs cost the same as one.

    Sharing data between variants is not always possible. Warm-up, entry
    timeframe, ATR period and holding window determine WHAT exactly is
    read; a variant changing any of them reads a different series.
    Such a sweep is rejected at the input, not computed on someone else's
    data (P-001).
    """
    if not variants:
        raise ValueError("sweep without variants")

    shape = {
        (
            v.entry_timeframe,
            warmup_bars_a(v),
            v.stop_atr_period,
            v.time_stop_hours,
        )
        for _, v in variants
    }
    if len(shape) > 1:
        raise ValueError(
            "sweep variants require different data (timeframe, warm-up, "
            "ATR period or holding window): a shared pass is impossible"
        )
    if funnel is not None and len(variants) > 1:
        raise ValueError("the funnel is computed for a single variant, not for a sweep")

    reference = variants[0][1]
    membership = await universe_membership()
    available = await stream_months("setup_a", settings)
    names = symbols if symbols is not None else sorted(membership)

    results = {
        label: RunResult(setup="setup_a", symbols_considered=len(names))
        for label, _ in variants
    }
    instruments = await load_instrument_params(names)
    slip = slippage_pct if slippage_pct is not None else Decimal(str(settings.costs.slippage_pct))
    warmup = warmup_bars_a(reference)
    entry_interval = reference.entry_interval

    cfg = BacktestConfig(
        initial_equity=Decimal(str(settings.risk.equity_usd)),
        taker_fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
        slippage_pct=slip,
        time_stop=timedelta(hours=reference.time_stop_hours),
        warmup_bars=warmup,
    )

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

        # Warm-up is taken BEFORE the window start: otherwise the first seven days
        # of in-sample drop out of the measurement, because the volume z-score
        # has not yet filled its window.
        padded = start - entry_interval * (warmup + 1)
        bars = await load_bars(symbol, reference.entry_timeframe, padded, end)
        if len(bars) < warmup + 1:
            continue

        hourly = await load_bars(symbol, "1h", padded - timedelta(days=7), end)
        atr_series = build_atr_series(
            hourly, reference.stop_atr_period, timedelta(hours=1)
        )
        if not atr_series.values:
            for result in results.values():
                result.rejections["MISSING_ATR"] = (
                    result.rejections.get("MISSING_ATR", 0) + 1
                )
            continue

        context = await load_context(symbol, padded, end)
        mmr, qty_step, min_qty, min_notional = spec

        for label, params in variants:
            result = results[label]
            signal_fn = make_signal_fn_a(context, params, atr_series, funnel)
            rejections: dict[str, int] = {}
            sizer = make_sizer(
                settings,
                mmr=mmr,
                qty_step=qty_step,
                min_qty=min_qty,
                min_notional=min_notional,
                equity=Decimal(str(settings.risk.equity_usd)),
                rejections=rejections,
            )

            symbol_result = run(bars, signal_fn, sizer, cfg, symbol=symbol)

            # Point-in-time: a trade counts only if the symbol was
            # in the universe AND all streams were available in THIS month.
            # Bars before `start` serve only as indicator warm-up.
            opened = [o for o in symbol_result.outcomes if o.trade.opened_at >= start]
            kept = [
                o
                for o in opened
                if f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}" in months
            ]
            if kept:
                result.per_symbol[symbol] = kept
                result.outcomes.extend(kept)
                result.symbols_traded += 1

            if funnel is not None:
                # The tail of the funnel is counted by the RUNNER: the signal
                # function knows neither about the risk manager nor about the
                # universe composition. Without these two stages the whole gap
                # between "signal issued" and "trade" is attributed to the risk
                # manager, which is wrong: the very misreading the funnel exists for.
                for _ in opened:
                    funnel.enter("funded")
                for outcome in kept:
                    funnel.enter("in_universe")
                    # Stops of trades that the risk manager SKIPPED.
                    # Comparing with the distribution over all signals
                    # shows not only how much was lost, but also that
                    # what was lost is not random.
                    trade = outcome.trade
                    if trade.entry > 0:
                        funnel.record_funded(trade.risk_per_unit / trade.entry * 100)
            for code, n in rejections.items():
                result.rejections[code] = result.rejections.get(code, 0) + n

    return results


async def run_setup_a(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    params: SetupAParams | None = None,
    funnel: Funnel | None = None,
) -> RunResult:
    """Setup A run over the universe. Entry on 15m, ATR from hourly bars.

    There are two differences from Setup B, both forced.

    First: ATR is computed on a SEPARATE hourly series and passed in
    ready-made. Recomputing it on each of seventy thousand 15-minute
    bars is not possible: it is quadratic, and the run never finishes.

    Second: admission months are intersected with the stream availability matrix.
    Setup A needs the `kline_15m` stream, which some symbols lack even though
    they passed universe selection by hourly-candle turnover.
    """
    params = params or SetupAParams.from_config(settings.strategy["setup_a"])
    results = await sweep_setup_a(
        settings,
        start,
        end,
        [("baseline", params)],
        slippage_pct=slippage_pct,
        symbols=symbols,
        funnel=funnel,
    )
    return results["baseline"]



async def sweep_setup_c(
    settings: Settings,
    start: datetime,
    end: datetime,
    variants: list[tuple[str, SetupCParams]],
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    funnel: Funnel | None = None,
    breakeven_at_r: Decimal | None = None,
) -> dict[str, RunResult]:
    """Setup C: several parameter sets in one pass over the data.

    Entry and ATR are on one timeframe, so one series is read, not two.
    Warm-up and holding window may differ between variants: both
    affect only the computation, not which bars are read, unlike
    Setup A, where warm-up sets the boundary of the database query.
    """
    if not variants:
        raise ValueError("sweep without variants")
    shapes = {(v.entry_timeframe, v.stop_atr_period) for _, v in variants}
    if len(shapes) > 1:
        raise ValueError(
            "variants require different series (timeframe or ATR period): "
            "a shared pass is impossible"
        )
    if funnel is not None and len(variants) > 1:
        raise ValueError("the funnel is computed for a single variant, not for a sweep")

    reference = variants[0][1]
    membership = await universe_membership()
    available = await stream_months("setup_c", settings)
    names = symbols if symbols is not None else sorted(membership)

    results = {
        label: RunResult(setup="setup_c", symbols_considered=len(names))
        for label, _ in variants
    }
    instruments = await load_instrument_params(names)
    slip = slippage_pct if slippage_pct is not None else Decimal(str(settings.costs.slippage_pct))
    interval = reference.entry_interval
    max_warmup = max(warmup_bars_c(v) for _, v in variants)

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

        padded = start - interval * (max_warmup + 1)
        bars = await load_bars(symbol, reference.entry_timeframe, padded, end)
        if len(bars) < max_warmup + 1:
            continue

        atr_series = build_atr_series(bars, reference.stop_atr_period, interval)
        if not atr_series.values:
            for result in results.values():
                result.rejections["MISSING_ATR"] = (
                    result.rejections.get("MISSING_ATR", 0) + 1
                )
            continue

        context = await load_context(symbol, padded, end)
        schedule = await load_funding_schedule(symbol, padded, end)
        mmr, qty_step, min_qty, min_notional = spec

        for label, params in variants:
            result = results[label]
            cfg = BacktestConfig(
                initial_equity=Decimal(str(settings.risk.equity_usd)),
                taker_fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
                slippage_pct=slip,
                time_stop=timedelta(hours=params.time_stop_hours),
                warmup_bars=warmup_bars_c(params),
                breakeven_at_r=breakeven_at_r,
            )
            signal_fn = make_signal_fn_c(context, params, atr_series, funnel)
            rejections: dict[str, int] = {}
            sizer = make_sizer(
                settings,
                mmr=mmr,
                qty_step=qty_step,
                min_qty=min_qty,
                min_notional=min_notional,
                equity=Decimal(str(settings.risk.equity_usd)),
                rejections=rejections,
            )
            symbol_result = run(
                bars, signal_fn, sizer, cfg, symbol=symbol, funding=schedule
            )

            opened = [o for o in symbol_result.outcomes if o.trade.opened_at >= start]
            kept = [
                o
                for o in opened
                if f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}" in months
            ]
            if kept:
                result.per_symbol[symbol] = kept
                result.outcomes.extend(kept)
                result.symbols_traded += 1
            if funnel is not None:
                for _ in opened:
                    funnel.enter("funded")
                for outcome in kept:
                    funnel.enter("in_universe")
                    trade = outcome.trade
                    if trade.entry > 0:
                        funnel.record_funded(trade.risk_per_unit / trade.entry * 100)
            for code, n in rejections.items():
                result.rejections[code] = result.rejections.get(code, 0) + n

    return results


async def random_entry_benchmark(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    signal_rate: float,
    long_share: float,
    seeds: tuple[int, ...] = (1, 2, 3, 4, 5),
    params: SetupCParams | None = None,
    breakeven_at_r: Decimal | None = None,
) -> dict[int, RunResult]:
    """The same run, but with a coin flip instead of the entry condition.

    Null hypothesis: the positive E comes not from the trigger but from the
    exit construction: an ATR stop, a 2R target and the asymmetry of the move
    distribution. Several seeds are needed because one gives one number,
    while the question is about the spread.
    """
    from src.backtest.benchmark import RandomEntryParams, make_random_signal_fn

    params = params or SetupCParams.from_config(settings.strategy["setup_c"])
    membership = await universe_membership()
    available = await stream_months("setup_c", settings)
    names = sorted(membership)
    instruments = await load_instrument_params(names)
    interval = params.entry_interval
    warmup = warmup_bars_c(params)

    results = {
        seed: RunResult(setup="random entry", symbols_considered=len(names))
        for seed in seeds
    }
    cfg = BacktestConfig(
        initial_equity=Decimal(str(settings.risk.equity_usd)),
        taker_fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
        slippage_pct=Decimal(str(settings.costs.slippage_pct)),
        time_stop=timedelta(hours=params.time_stop_hours),
        warmup_bars=warmup,
        breakeven_at_r=breakeven_at_r,
    )

    for symbol in names:
        months = membership.get(symbol, set()) & available.get(symbol, set())
        if not months:
            continue
        spec = instruments.get(symbol)
        if spec is None:
            continue
        padded = start - interval * (warmup + 1)
        bars = await load_bars(symbol, params.entry_timeframe, padded, end)
        if len(bars) < warmup + 1:
            continue
        atr_series = build_atr_series(bars, params.stop_atr_period, interval)
        if not atr_series.values:
            continue
        # Same accruals as the setup's. Otherwise the comparison stops
        # being a comparison: one side pays for holding, the other
        # holds for free, and the difference is no longer about the entry.
        schedule = await load_funding_schedule(symbol, padded, end)
        mmr, qty_step, min_qty, min_notional = spec

        for seed in seeds:
            result = results[seed]
            signal_fn = make_random_signal_fn(
                params,
                RandomEntryParams(
                    signal_rate=signal_rate, long_share=long_share, seed=seed
                ),
                atr_series,
                symbol=symbol,
            )
            rejections: dict[str, int] = {}
            sizer = make_sizer(
                settings,
                mmr=mmr,
                qty_step=qty_step,
                min_qty=min_qty,
                min_notional=min_notional,
                equity=Decimal(str(settings.risk.equity_usd)),
                rejections=rejections,
            )
            symbol_result = run(
                bars, signal_fn, sizer, cfg, symbol=symbol, funding=schedule
            )
            kept = [
                o
                for o in symbol_result.outcomes
                if o.trade.opened_at >= start
                and f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}" in months
            ]
            if kept:
                result.per_symbol[symbol] = kept
                result.outcomes.extend(kept)
                result.symbols_traded += 1

    return results


async def run_setup_c(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    params: SetupCParams | None = None,
    funnel: Funnel | None = None,
    breakeven_at_r: Decimal | None = None,
) -> RunResult:
    """Setup C run over the universe."""
    params = params or SetupCParams.from_config(settings.strategy["setup_c"])
    results = await sweep_setup_c(
        settings,
        start,
        end,
        [("baseline", params)],
        slippage_pct=slippage_pct,
        symbols=symbols,
        funnel=funnel,
        breakeven_at_r=breakeven_at_r,
    )
    return results["baseline"]



async def sweep_setup_d(
    settings: Settings,
    start: datetime,
    end: datetime,
    variants: list[tuple[str, SetupDParams]],
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    funnel: Funnel | None = None,
    coin_flip_seed: int | None = None,
    stream_setup: str = "setup_d",
) -> dict[str, RunResult]:
    """Setup D: daily bars, time exit. DEC-033.

    `stream_setup` says whose stream requirements to apply. Setup E
    reuses rule D with its own config section (DEC-038).

    Daily bars are built from 4h and only complete ones (resample). ATR is on
    daily bars, the series key is the close time of the day.

    Breakeven is not passed: it contradicts the hypothesis
    "hold for a week" (DEC-033).

    `coin_flip_seed` is the null hypothesis: the same entries and exits,
    direction by coin flip. Each symbol gets its own seed.
    """
    from src.backtest.resample import DAY, to_daily

    if not variants:
        raise ValueError("sweep without variants")
    if len({v.stop_atr_period for _, v in variants}) > 1:
        raise ValueError("variants with different ATR periods read different series")
    if funnel is not None and len(variants) > 1:
        raise ValueError("the funnel is computed for a single variant, not for a sweep")

    reference = variants[0][1]
    membership = await universe_membership()
    available = await stream_months(stream_setup, settings)
    names = symbols if symbols is not None else sorted(membership)
    results = {
        label: RunResult(setup=stream_setup, symbols_considered=len(names))
        for label, _ in variants
    }
    instruments = await load_instrument_params(names)
    slip = slippage_pct if slippage_pct is not None else Decimal(str(settings.costs.slippage_pct))
    max_warmup = max(warmup_bars_d(v) for _, v in variants)

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

        padded = start - DAY * (max_warmup + 5)
        raw = await load_bars(symbol, "4h", padded, end)
        bars = to_daily(raw, timedelta(hours=4))
        if len(bars) < max_warmup + 1:
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
                warmup_bars=warmup_bars_d(params),
            )
            seed = f"{coin_flip_seed}:{symbol}" if coin_flip_seed is not None else None
            signal_fn = make_signal_fn_d(params, atr_series, funnel, coin_flip_seed=seed)
            rejections: dict[str, int] = {}
            sizer = make_sizer(
                settings,
                mmr=mmr,
                qty_step=qty_step,
                min_qty=min_qty,
                min_notional=min_notional,
                equity=Decimal(str(settings.risk.equity_usd)),
                rejections=rejections,
            )
            symbol_result = run(
                bars, signal_fn, sizer, cfg, symbol=symbol, funding=schedule
            )
            opened = [o for o in symbol_result.outcomes if o.trade.opened_at >= start]
            kept = [
                o
                for o in opened
                if f"{o.trade.opened_at.year:04d}-{o.trade.opened_at.month:02d}" in months
            ]
            if kept:
                result.per_symbol[symbol] = kept
                result.outcomes.extend(kept)
                result.symbols_traded += 1
            if funnel is not None:
                for _ in opened:
                    funnel.enter("funded")
                for outcome in kept:
                    funnel.enter("in_universe")
                    trade = outcome.trade
                    if trade.entry > 0:
                        funnel.record_funded(trade.risk_per_unit / trade.entry * 100)
            for code, n in rejections.items():
                result.rejections[code] = result.rejections.get(code, 0) + n

    return results


async def run_setup_d(
    settings: Settings,
    start: datetime,
    end: datetime,
    *,
    slippage_pct: Decimal | None = None,
    symbols: list[str] | None = None,
    params: SetupDParams | None = None,
    funnel: Funnel | None = None,
    coin_flip_seed: int | None = None,
    breakeven_at_r: Decimal | None = None,
    stream_setup: str = "setup_d",
) -> RunResult:
    """Setup D run (and E: the same rule, its own config section).

    `breakeven_at_r` is accepted for the shared runner signature
    and IGNORED: breakeven does not apply to D and E."""
    del breakeven_at_r
    params = params or SetupDParams.from_config(settings.strategy[stream_setup])
    results = await sweep_setup_d(
        settings,
        start,
        end,
        [("baseline", params)],
        slippage_pct=slippage_pct,
        symbols=symbols,
        funnel=funnel,
        coin_flip_seed=coin_flip_seed,
        stream_setup=stream_setup,
    )
    return results["baseline"]


#: MMR, lot step, minimum lot, minimum notional.
InstrumentParams = tuple[Decimal, Decimal, Decimal, Decimal]


async def load_instrument_params(symbols: list[str]) -> dict[str, InstrumentParams]:
    """Instrument parameters from the API cache. Hardcoding is forbidden (spec §16)."""
    from src.core.db import Instrument

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(
                    Instrument.symbol,
                    Instrument.mmr,
                    Instrument.qty_step,
                    Instrument.min_order_qty,
                    Instrument.min_notional_value,
                ).where(Instrument.symbol.in_(symbols))
            )
        ).all()
    return {
        r[0]: (r[1] or Decimal("0.005"), r[2], r[3], r[4] or Decimal("5")) for r in rows
    }


__all__ = [
    "RunResult",
    "SymbolRun",
    "load_bars",
    "load_funding_schedule",
    "run_setup_a",
    "random_entry_benchmark",
    "run_setup_c",
    "run_setup_d",
    "stream_months",
    "sweep_setup_a",
    "sweep_setup_c",
    "sweep_setup_d",
    "load_context",
    "load_instrument_params",
    "run_setup_b",
    "summarize",
    "universe_membership",
]
