"""Postgres connection and the declarative base.

TimescaleDB hypertables (ohlcv, funding_history, open_interest,
liquidations) arrive in Phase 1b. Here only what the Instrument
Filter needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src.core.config import get_env


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    """Cache of instrument parameters.

    Spec §16 forbids hardcoding min_notional, qty_step and MMR — this table
    is exactly where they are stored from the API. `updated_at` is needed to
    see staleness: the exchange changes parameters without warning.

    Numeric, not float: the values are used to round the order size, and a
    binary fraction error here turns into an exchange rejection.
    """

    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    base_coin: Mapped[str] = mapped_column(String(16))
    quote_coin: Mapped[str] = mapped_column(String(16))
    contract_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))

    # Asset class from the API: "" — crypto, "innovation" — innovation-zone
    # crypto, "stock" — tokenized stocks, "commodity" — commodities.
    # The DEC-002 filter works on it.
    symbol_type: Mapped[str] = mapped_column(String(32), default="")

    tick_size: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    qty_step: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    min_order_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    max_order_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    min_notional_value: Mapped[Decimal] = mapped_column(Numeric(38, 18))

    max_leverage: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    funding_interval_minutes: Mapped[int] = mapped_column()
    launch_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    mmr: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    initial_margin_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    risk_limit_value: Mapped[Decimal | None] = mapped_column(Numeric(38, 8), nullable=True)

    is_delisted: Mapped[bool] = mapped_column(default=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Time series. TimescaleDB hypertables.
# ---------------------------------------------------------------------------
# In all tables below time is part of the primary key: Timescale requires
# it of any unique index on a hypertable, and we need it anyway for an
# idempotent upsert on the natural key.


class Ohlcv(Base):
    """Candles. The turnover source for the point-in-time universe (DEC-003)."""

    __tablename__ = "ohlcv"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    open: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    high: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    low: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    close: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    volume: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    # Turnover in USDT. It, not volume in the base coin, ranks the universe:
    # volume in coins is not comparable across symbols.
    turnover: Mapped[Decimal] = mapped_column(Numeric(38, 18))


class FundingHistory(Base):
    """Funding history.

    `interval_minutes` is stored next to the rate because it differs between
    symbols (BTC — 480, some alts 240 or 60), and percentiles over raw values
    are not comparable across pairs. `rate_8h` is the normalized rate,
    computed on write so that the strategy does not recompute it every time
    and does not diverge from the backtest in the normalization method.
    """

    __tablename__ = "funding_history"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    rate: Mapped[Decimal] = mapped_column(Numeric(20, 12))
    interval_minutes: Mapped[int] = mapped_column(Integer)
    rate_8h: Mapped[Decimal] = mapped_column(Numeric(20, 12))


class OpenInterest(Base):
    """Open Interest.

    The basis of the Setup A trigger: OI contraction over an hour in the
    direction of the price move (DEC-001). Liquidations are not part of the trigger.
    """

    __tablename__ = "open_interest"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    open_interest: Mapped[Decimal] = mapped_column(Numeric(38, 18))


class Liquidation(Base):
    """Liquidations from the WebSocket.

    A research dataset, written from the moment the collector starts.
    It does NOT take part in trading logic or the backtest (DEC-001): replacing
    the OI trigger with real liquidations would drive the backtest apart from
    live trading and require a full re-backtest from scratch.

    The goal — in half a year, measure whether the OI trigger correlates
    with actual cascades.
    """

    __tablename__ = "liquidations"

    # Surrogate key: two events of a cascade can match on all fields,
    # and there is no natural key here.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OrderbookSnapshot(Base):
    """Order book snapshot once a minute for universe symbols.

    Like liquidations — a research dataset (DEC-007). It does NOT take part in
    trading logic, setup definitions or the backtest.

    Collected for two things that will be needed in half a year: the real
    spread distribution across the universe and the ability to check how
    well the Abdi–Ranaldo estimate matched reality.

    `spread_pct` is computed on write, not on read: it is the dataset's main
    quantity, and storing it ready-made is cheaper than deriving it from
    bid/ask every time.

    Depth is stored as dollar aggregates, not raw levels: 100 symbols once
    a minute is 144 thousand rows a day, and JSONB with fifty levels would
    blow the dataset up to tens of gigabytes over the period, adding nothing
    to the questions it is collected for.
    """

    __tablename__ = "orderbook_snapshots"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    best_bid: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    best_ask: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    best_bid_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    best_ask_qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))

    #: Actual spread as a percentage of the mid.
    spread_pct: Mapped[Decimal] = mapped_column(Numeric(18, 8))

    #: Total depth in USD over the first N levels. Answers the question
    #: "how much of the book will our $70 order eat".
    bid_depth_5_usd: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    ask_depth_5_usd: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    bid_depth_20_usd: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    ask_depth_20_usd: Mapped[Decimal] = mapped_column(Numeric(38, 8))

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ---------------------------------------------------------------------------
# Collection service tables
# ---------------------------------------------------------------------------
class StreamAvailability(Base):
    """Availability matrix (symbol, month, stream).

    Availability cannot be computed per symbol: MULTIUSDT serves candles but
    not OI and funding. A symbol's admission to a setup for a month is the
    intersection of the streams the setup requires.

    Without this matrix the backtest either silently skips trades or crashes
    on None in the middle of a run.
    """

    __tablename__ = "stream_availability"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    stream: Mapped[str] = mapped_column(String(16), primary_key=True)

    available: Mapped[bool] = mapped_column(Boolean, default=False)
    bar_count: Mapped[int] = mapped_column(Integer, default=0)
    expected_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class StreamStartRow(Base):
    """Start date of each stream per symbol.

    Per stream rather than a single listing date: for some pairs OI starts
    earlier than candles (ETHUSDT — OI from 2020-10, candles from 2021-03),
    so a single date would be wrong for at least one of them.

    `effective_start` for a set of required streams is computed as the
    maximum of their `first_ts`.
    """

    __tablename__ = "stream_starts"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    stream: Mapped[str] = mapped_column(String(16), primary_key=True)

    first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class CollectorCheckpoint(Base):
    """Resume point and watchdog input.

    `last_success_at` is not a convenience but a liveness signal: without it
    a collector that silently stopped updating is indistinguishable from a
    collector that has nothing to do. This exact class of failures ("process
    alive, data stale") is what the watchdog catches.
    """

    __tablename__ = "collector_checkpoints"

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    stream: Mapped[str] = mapped_column(String(16), primary_key=True)

    #: Bounds of the ALREADY COVERED range, both.
    #:
    #: A single front bound is not enough. With it a request for greater depth
    #: after a shallow run silently does nothing and reports "0 rows" — i.e.
    #: it looks like "already collected", although the history was not fully
    #: downloaded. Exactly this happened on the SOLUSDT trial run: 18 days of
    #: data with a checkpoint pointing at today.
    first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class LivePosition(Base):
    """A position opened by the trading cycle (Phase 5, DEC-041).

    The process's memory between runs: which positions are ours, under which
    keys their orders are and when to close them by time. The truth about
    what is actually open is held by the exchange; this table is reconciled
    with it at the start of every cycle, and a mismatch turns trading off (P-001).
    """

    __tablename__ = "live_positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    setup: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    is_long: Mapped[bool] = mapped_column(Boolean)
    qty: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    #: Before execution — the planned price, after — the average fill price.
    entry_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    stop_price: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    margin: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    entry_fee: Mapped[Decimal] = mapped_column(Numeric(38, 18), default=0)
    entry_link_id: Mapped[str] = mapped_column(String(40), unique=True)
    stop_link_id: Mapped[str] = mapped_column(String(40))
    target_link_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: The daily bar on whose close the decision was made.
    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: Entry decision moment (00:00 UTC).
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    close_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: pending — written BEFORE the entry is sent (write-ahead record journal);
    #: open; closed; void — the entry did not happen.
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    pnl: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)


class LiveState(Base):
    """Trading mode and loss reference points — per setup.

    The weekly stop is cleared only manually (spec §5) and must survive a
    process restart. Stored here, not in memory.
    """

    __tablename__ = "live_state"

    setup: Mapped[str] = mapped_column(String(16), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    equity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    day: Mapped[str] = mapped_column(String(10))
    day_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    week: Mapped[str] = mapped_column(String(8))
    week_equity: Mapped[Decimal] = mapped_column(Numeric(38, 18))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LiveJournal(Base):
    """Journal of cycle decisions: what it saw, what it decided and why.

    A rejection with a reason code leaves a trace (P-001); a silent skip does
    not, and later it is impossible to understand why the bot opened or did
    not open a position.
    """

    __tablename__ = "live_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    setup: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(24))
    code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)


class BotRun(Base):
    """A bot platform run (DEC-049): what it received, what it did, what it handed over.

    The board is the only channel between bots: the next bot reads the previous
    one's result from here, the panel — each bot's window and its charts.
    """

    __tablename__ = "bot_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bot: Mapped[str] = mapped_column(String(24), index=True)
    book: Mapped[str] = mapped_column(String(32), index=True)
    #: Decision moment (00:00 UTC) the run belongs to.
    moment: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    #: night — the decision cycle, day — day watch.
    shift: Mapped[str] = mapped_column(String(8), default="night")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: running / ok / refused / failed / skipped
    status: Mapped[str] = mapped_column(String(12), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    inputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    outputs: Mapped[dict] = mapped_column(JSONB, default=dict)
    #: Numbers for charts: processed, rejections, seconds, etc.
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class DecisionTrace(Base):
    """A candidate's path through the bots: stage, verdict, reason code (DEC-049).

    For every symbol of the day — signal, risk, execution, verification. Answers
    the question "did the trade go through or not, and at which step was it dropped".
    """

    __tablename__ = "decision_trace"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(32), index=True)
    moment: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    #: signal / risk / execution / verify
    stage: Mapped[str] = mapped_column(String(12))
    #: pass / reject / fail / info
    verdict: Mapped[str] = mapped_column(String(8))
    code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    #: rule — rejection by a strategy or risk rule; platform — rejection by the
    #: platform itself (data, window, reconciliation). Without the split the
    #: sample of 60 trades is distorted (design judges, DEC-049).
    origin: Mapped[str] = mapped_column(String(10), default="rule")
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    bot_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OrderIntent(Base):
    """Risk Manager's entry approval — the second key (DEC-049).

    The Executor enters only if its own decision matched the approval:
    one key — "no", two — "yes".
    """

    __tablename__ = "order_intents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(32), index=True)
    moment: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    #: approve / reject
    verdict: Mapped[str] = mapped_column(String(8))
    code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_long: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    qty: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(38, 18), nullable=True)
    bot_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("book", "moment", "symbol", name="uq_intent_day_symbol"),)


class UniverseSnapshot(Base):
    """Universe composition by month (DEC-003, condition 4).

    Needed for reproducibility: without it, a month later you cannot tell
    why the backtest gave exactly these numbers, and a repeat run may
    differ.

    `turnover_prev_period` stresses the shift: the universe for month M is
    computed from the turnover of month M−1, never from the turnover of the
    traded period.
    """

    __tablename__ = "universe_snapshots"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)

    turnover_prev_period: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    avg_daily_turnover: Mapped[Decimal] = mapped_column(Numeric(38, 8))
    median_spread_pct: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    rank: Mapped[int] = mapped_column(Integer)

    included: Mapped[bool] = mapped_column(Boolean, default=False)
    # Code from RejectionCode. Empty for included ones.
    rejection: Mapped[str | None] = mapped_column(String(32), nullable=True)
    listing_age_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


Index("ix_universe_snapshots_month_rank", UniverseSnapshot.month, UniverseSnapshot.rank)
Index("ix_ohlcv_symbol_tf_ts", Ohlcv.symbol, Ohlcv.timeframe, Ohlcv.ts.desc())


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_env().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
