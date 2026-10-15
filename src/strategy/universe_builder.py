"""Assembling the point-in-time universe from the collected data.

The chicken-and-egg problem is solved like this: first the light 4h candles
are downloaded for all candidates, a monthly ranking is computed from their
`turnover`, and only for the selected symbols is the full 15m/1h/OI/funding
depth downloaded.

The cost difference is large: 4h for 513 symbols is about 2.6 thousand
requests, the full depth for the same symbols is close to a hundred thousand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select

from src.core.config import Settings, UniverseConfig
from src.core.db import Instrument, Ohlcv, UniverseSnapshot, session_scope
from src.core.logging import get_logger
from src.core.models import DataStream, InstrumentSpec
from src.exchange.instrument_filter import assess
from src.strategy.universe import (
    SymbolMonthStats,
    UniverseEntry,
    build_universe,
    months_between,
    previous_month,
)

log = get_logger(__name__)


async def monthly_turnover(timeframe: str = "4h") -> dict[str, dict[str, tuple[Decimal, int]]]:
    """Turnover and bar count per symbol and month.

    Returns {symbol: {month: (turnover, bars)}}. Aggregation is done in
    Postgres: there are millions of raw bars, and no reason to pull them into Python.
    """
    bucket = func.to_char(Ohlcv.ts, "YYYY-MM").label("month")
    query = (
        select(Ohlcv.symbol, bucket, func.sum(Ohlcv.turnover), func.count())
        .where(Ohlcv.timeframe == timeframe)
        .group_by(Ohlcv.symbol, bucket)
    )

    async with session_scope() as session:
        rows = (await session.execute(query)).all()

    out: dict[str, dict[str, tuple[Decimal, int]]] = {}
    for symbol, month, turnover, count in rows:
        out.setdefault(symbol, {})[month] = (Decimal(turnover or 0), count)
    return out


async def instrument_specs() -> dict[str, InstrumentSpec]:
    """Instrument parameters from the API cache, without touching the network.

    Needed for the tradability verdict for EVERY month. Only what does not
    depend on time comes from the API — lot step, minimum order, asset class;
    the price is taken from the candles of the selection month (see
    monthly_price).
    """
    async with session_scope() as session:
        rows = (await session.execute(select(Instrument))).scalars().all()
    out: dict[str, InstrumentSpec] = {}
    for row in rows:
        out[row.symbol] = InstrumentSpec(
            symbol=row.symbol,
            base_coin=row.base_coin,
            quote_coin=row.quote_coin,
            contract_type=row.contract_type,
            status=row.status,
            symbol_type=row.symbol_type,
            tick_size=row.tick_size,
            qty_step=row.qty_step,
            min_order_qty=row.min_order_qty,
            max_order_qty=row.max_order_qty,
            min_notional_value=row.min_notional_value,
            max_leverage=row.max_leverage,
            funding_interval_minutes=row.funding_interval_minutes,
            launch_time=row.launch_time,
            is_delisted=row.is_delisted,
        )
    return out


def tradable_in_month(
    specs: dict[str, InstrumentSpec],
    prices: dict[tuple[str, str], Decimal],
    symbols: list[str],
    selection_month: str,
    settings: Settings,
) -> frozenset[str]:
    """Who was tradable with OUR deposit in the selection month.

    The verdict depends on the price, and the price depends on the month.
    Taking it live from the exchange means recomputing history against today's
    market: a symbol that got more expensive drops out of the year before last
    retroactively, and rebuilding the snapshot gives a different composition (DEC-015).
    """
    out: set[str] = set()
    for symbol in symbols:
        spec = specs.get(symbol)
        price = prices.get((symbol, selection_month))
        if spec is None or price is None or price <= 0:
            # Nothing to check with. The calling code decides via
            # `unverifiable`: "checked and not possible" and "nothing to
            # check with" are different things (P-001).
            continue
        if assess(spec, price, Decimal(0), settings).tradable:
            out.add(symbol)
    return frozenset(out)


async def monthly_price(timeframe: str = "4h") -> dict[tuple[str, str], Decimal]:
    """Median close price per symbol and month.

    Needed for the tradability verdict. It used to be taken from the LIVE price
    on the exchange — and thereby made the historical universe composition
    depend on today: ETHUSDT got more expensive, stopped being divisible into
    three parts at a $70 notional and dropped out of 2024 retroactively.

    This broke the promise of DEC-003, condition 4: the snapshot was introduced
    for reproducibility, yet a rebuild gave a different composition. A quiet
    defect — the composition changes, all the backtest numbers drift, and not a
    single check turns red.

    Median, not the last close: a month with one outlier must not decide the
    symbol's fate.
    """
    bucket = func.to_char(Ohlcv.ts, "YYYY-MM").label("month")
    median = func.percentile_cont(0.5).within_group(Ohlcv.close.asc())
    query = (
        select(Ohlcv.symbol, bucket, median)
        .where(Ohlcv.timeframe == timeframe)
        .group_by(Ohlcv.symbol, bucket)
    )
    async with session_scope() as session:
        rows = (await session.execute(query)).all()
    return {
        (symbol, month): Decimal(str(price))
        for symbol, month, price in rows
        if price is not None and Decimal(str(price)) > 0
    }


async def symbols_with_data(timeframe: str = "4h") -> set[str]:
    """Symbols that have any candles at all."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Ohlcv.symbol).where(Ohlcv.timeframe == timeframe).distinct()
            )
        ).scalars().all()
    return set(rows)


async def listing_dates(timeframe: str = "4h") -> dict[str, datetime]:
    """Listing dates: from the instrument cache, otherwise from the first candle.

    Delisted pairs have no `launch_time` — they are absent from `instruments-info`.
    They must not be dropped for that: the age rule would then silently bring back
    the survivorship bias that DEC-006 removes.

    The date of the first available candle is a correct substitute for our
    question. The rule asks "does the symbol have 90 days of history before the
    selection month", and the first candle answers that more accurately than no
    answer at all. It is a lower bound on age: the real listing may have been
    before the start of our window, in which case the estimate only understates
    the age, i.e. errs on the strict side.
    """
    async with session_scope() as session:
        declared = dict(
            (
                await session.execute(
                    select(Instrument.symbol, Instrument.launch_time).where(
                        Instrument.launch_time.is_not(None)
                    )
                )
            ).all()
        )
        first_bars = dict(
            (
                await session.execute(
                    select(Ohlcv.symbol, func.min(Ohlcv.ts))
                    .where(Ohlcv.timeframe == timeframe)
                    .group_by(Ohlcv.symbol)
                )
            ).all()
        )

    out = dict(first_bars)
    # The declared date is more accurate than the inferred one, so it wins.
    out.update(declared)
    return out


async def available_streams_by_month() -> dict[tuple[str, str], frozenset[DataStream]]:
    """Which streams are available per (symbol, month) triple.

    Empty until the availability matrix is built. At the selection stage on 4h
    candles this is fine: stream requirements apply only to the
    `universe_ranking`.
    """
    from src.core.db import StreamAvailability

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(
                    StreamAvailability.symbol,
                    StreamAvailability.month,
                    StreamAvailability.stream,
                ).where(StreamAvailability.available.is_(True))
            )
        ).all()

    out: dict[tuple[str, str], set[DataStream]] = {}
    for symbol, month, stream in rows:
        try:
            out.setdefault((symbol, month), set()).add(DataStream(stream))
        except ValueError:
            log.error("universe.unknown_stream", symbol=symbol, stream=stream)
    return {k: frozenset(v) for k, v in out.items()}


@dataclass(frozen=True)
class UniverseBuildResult:
    entries: list[UniverseEntry]
    months: list[str]

    @property
    def included(self) -> list[UniverseEntry]:
        return [e for e in self.entries if e.included]

    def selected_symbols(self) -> list[str]:
        """Union of the monthly compositions — what to download at full depth."""
        return sorted({e.symbol for e in self.entries if e.included})

    def months_by_symbol(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.entries:
            if e.included:
                counts[e.symbol] = counts.get(e.symbol, 0) + 1
        return counts


async def build_all_months(
    start: datetime,
    end: datetime,
    config: UniverseConfig,
    *,
    required_streams: frozenset[DataStream] = frozenset(),
    tradable: frozenset[str] | None = None,
    unverifiable: frozenset[str] = frozenset(),
    persist: bool = True,
) -> UniverseBuildResult:
    """Builds the universe for every month of the period and saves the snapshots.

    `tradable` is the Instrument Filter verdict. It is passed in from outside
    rather than derived here: it depends on the current price, which the
    instruments table does not have, and reconstructing it from min_notional
    would be guesswork. Without it BTCUSDT gets into the universe — it passes on
    turnover and age, but its lot step equals the whole position.
    """
    turnover = await monthly_turnover()
    listings = await listing_dates()
    streams = await available_streams_by_month()

    all_entries: list[UniverseEntry] = []
    months = months_between(start, end)

    for month in months:
        # DEC-003: lagged selection. The universe for month M is computed from
        # the turnover of month M−lag, never from the turnover of the traded period.
        selection_month = previous_month(month, config.selection_lag_months)

        stats: list[SymbolMonthStats] = []
        for symbol, by_month in turnover.items():
            data = by_month.get(selection_month)
            if data is None:
                continue
            total, bars = data
            stats.append(
                SymbolMonthStats(
                    symbol=symbol,
                    turnover=total,
                    bar_count=bars,
                    listing_date=listings.get(symbol),
                    available_streams=streams.get((symbol, selection_month), frozenset()),
                )
            )

        if not stats:
            continue

        entries = build_universe(
            month,
            stats,
            config,
            required_streams=required_streams,
            tradable=tradable,
            unverifiable=unverifiable,
        )
        all_entries.extend(entries)

        log.info(
            "universe.month",
            month=month,
            selection_month=selection_month,
            candidates=len(stats),
            included=sum(1 for e in entries if e.included),
        )

    if persist and all_entries:
        await _persist(all_entries)

    return UniverseBuildResult(entries=all_entries, months=months)


async def persist_entries(entries: list[UniverseEntry]) -> None:
    """Save an already built snapshot (a build with persist=False)."""
    if entries:
        await _persist(entries)


async def _persist(entries: list[UniverseEntry]) -> None:
    """Snapshots into the DB (DEC-003, condition 4).

    Without them, a month later there is no way to tell why the backtest gave
    exactly these numbers, and a rerun may differ.
    """
    from src.core.bulk import upsert_chunked

    rows = [
        {
            "month": e.month,
            "symbol": e.symbol,
            "turnover_prev_period": e.turnover_prev_period,
            "avg_daily_turnover": e.avg_daily_turnover,
            "median_spread_pct": None,
            "rank": e.rank,
            "included": e.included,
            "rejection": e.rejection.value if e.rejection else None,
            "listing_age_days": e.listing_age_days,
        }
        for e in entries
    ]
    await upsert_chunked(
        UniverseSnapshot,
        rows,
        index_elements=[UniverseSnapshot.month, UniverseSnapshot.symbol],
        update_columns=(
            "turnover_prev_period",
            "avg_daily_turnover",
            "rank",
            "included",
            "rejection",
            "listing_age_days",
        ),
    )
    log.info("universe.persisted", rows=len(rows))


__all__ = [
    "UniverseBuildResult",
    "available_streams_by_month",
    "build_all_months",
    "build_universe",
    "listing_dates",
    "monthly_turnover",
]
