"""Assembling the daily cycle from the database, config and exchange. Phase 5, DEC-041.

The cycle (`cycle.py`) is pure and tested on the paper exchange. Here is everything
that touches the database and the network: the month's universe, bars, instrument parameters,
correlations, funding and the choice of execution exchange.

Two execution exchanges:

* `paper` — paper on main-market prices (`paper_venue.py`).
  No keys needed; this is a forward test of the rule on new data;
* `testnet` — Bybit testnet. Needs testnet keys in `.env`; tests the
  order mechanics. The main exchange is unreachable by construction:
  the trading client refuses any address other than testnet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from src.backtest.evaluation import TIE_BREAK_SEED
from src.backtest.runner import load_bars, load_funding_schedule, load_instrument_params
from src.core.config import Settings
from src.execution.plan import InstrumentSpec
from src.live.cycle import CycleContext, CycleReport, EntryGate, Sizing, run_cycle
from src.live.lock import book_lock
from src.live.paper_venue import HOUR, MarketPaperVenue
from src.live.signals import Candidate, Skip, candidate_for, decision_moment, ordered
from src.live.store import DbStore, JournalEntry, ReadOnlyStore, Store
from src.risk.correlation import CorrelationSource
from src.risk.governor import GovernorLimits, TradingMode
from src.strategy.setup_d import SetupDParams, warmup_bars

DAY = timedelta(days=1)
PAPER_DIR = Path("var/paper")


class DataNotFresh(RuntimeError):
    """There is no data for a safe decision (P-001)."""


#: Paper executes the decision as if the night run had happened on time:
#: ten minutes after the daily close.
PAPER_DECISION_DELAY = timedelta(minutes=10)
#: A run earlier than this is on time: price marks are the same as at 00:10.
ON_TIME = timedelta(hours=1)

#: How much history to load for the signal. Wilder's ATR is recursive:
#: the value depends on which bar the count started from, and forgets the start
#: as (13/14)^n. The backtest computes it over years of history; on 39 days
#: (warm-up + 10) the live ATR diverged from the backtest by up to 70%, stops by up to
#: 2.5% of price, the AAVE size on 2026-09-28 came out 0.07 instead of 0.08 (night
#: re-check). Over 400 days the trace of the start is (13/14)^386 ≈ 4e-13.
LIVE_HISTORY_DAYS = 400


@dataclass
class Prepared:
    moment: datetime
    universe: list[str]
    candidates: list[Candidate]
    skips: dict[str, Skip] = field(default_factory=dict)
    #: The moment the decision is executed for. For paper on a late
    #: run — earlier than the actual run time.
    as_of: datetime | None = None
    #: A note on the decision for the journal (e.g. "the day's decision is already made").
    note: str | None = None
    #: Why there will be no entries at all (e.g. no universe for the month).
    #: Exits and reconciliation still run: blocking the close of
    #: open positions for lack of a list for NEW entries is not allowed.
    #: A cycle with a problem ends in failure — the night script will retry it.
    problem: str | None = None


def paper_as_of(now: datetime) -> datetime:
    """The execution moment of a paper decision.

    The Mac can sleep through the entry window: at night on battery it wakes up for
    seconds (on 2026-09-27 the cycle finished at 05:27 MSK). For paper this is
    no reason to skip the day — the price at the decision moment is recorded in hourly
    bars. The decision is made on data up to the daily close, executed
    at the daily close price, and stops are then replayed over bars up to
    the current hour. There is no future relative to the decision here: everything that
    happened after 00:10 affects only exits, just as in real life.

    This is not possible for the real exchange — there the price is the current one — and the entry
    window stays strict.
    """
    moment = decision_moment(now)
    # In the first hour after the daily close paper sees the close price anyway:
    # marks are taken from closed hourly bars. A run at 00:10:11 is
    # on time, not "later than scheduled" (on 2026-09-28 the journal marked it wrongly).
    if now < moment + ON_TIME:
        return now
    return moment + PAPER_DECISION_DELAY


async def universe_of(month: str) -> list[str]:
    from src.core.db import UniverseSnapshot, session_scope

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(UniverseSnapshot.symbol).where(
                    UniverseSnapshot.month == month, UniverseSnapshot.included.is_(True)
                )
            )
        ).scalars().all()
    return sorted(rows)


def month_of_decision(moment: datetime) -> str:
    """The universe month is the month of the BAR we decide on, as in the backtest.

    The backtest dates a trade by the start of the signal day and takes the universe
    of that month. A decision at 00:00 on the first of the month is made on the bar of
    the last day of the previous month — and its universe is the previous month's.
    """
    bar = moment - DAY
    return f"{bar.year:04d}-{bar.month:02d}"


async def prepare(settings: Settings, setup: str, now: datetime) -> Prepared:
    """The current month's universe and the signals on it at the decision moment."""
    moment = decision_moment(now)
    month = month_of_decision(moment)
    universe = await universe_of(month)
    if not universe:
        # Not a failure of the whole cycle: open positions must close by time exit
        # and by stop even without a list for new entries (night re-check
        # 2026-09-28: on October 2 the cycle would have crashed together with the exits).
        return Prepared(
            moment, [], [],
            problem=(
                f"no universe for {month} — no new entries. "
                "Run build-universe --month due"
            ),
        )
    params = SetupDParams.from_config(settings.strategy[setup])
    start = moment - DAY * max(LIVE_HISTORY_DAYS, warmup_bars(params) + 10)
    found: list[Candidate] = []
    skips: dict[str, Skip] = {}
    for symbol in universe:
        bars = await load_bars(symbol, "4h", start, moment)
        got = candidate_for(symbol, bars, params, moment)
        if isinstance(got, Skip):
            skips[symbol] = got
        else:
            found.append(got)
    return Prepared(moment, universe, ordered(found, TIE_BREAK_SEED), skips)


async def instrument_specs(symbols: list[str]) -> dict[str, InstrumentSpec]:
    from src.core.db import Instrument, session_scope

    async with session_scope() as s:
        rows = (
            await s.execute(select(Instrument).where(Instrument.symbol.in_(symbols)))
        ).scalars().all()
    return {
        r.symbol: InstrumentSpec(
            symbol=r.symbol, tick_size=r.tick_size, qty_step=r.qty_step,
            min_order_qty=r.min_order_qty, max_order_qty=r.max_order_qty,
            min_notional=r.min_notional_value,
        )
        for r in rows
        if r.status == "Trading"
    }


def _hour_floor(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


async def paper_venue(
    settings: Settings, setup: str, symbols: list[str], now: datetime
) -> tuple[MarketPaperVenue, Path, list[str]]:
    """The paper exchange brought up to `now` over hourly bars.

    `now` is the decision's execution moment (`paper_as_of`), not necessarily
    the current time: the exchange does not look beyond it.

    The stop of a held position is checked against every bar since the previous
    run. If bars are missing, it is unknown whether the stop triggered —
    and deciding on such a picture is not allowed (P-001).
    """
    path = PAPER_DIR / f"{setup}.json"
    venue = MarketPaperVenue.load(
        path,
        fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
        slippage_pct=Decimal(str(settings.costs.slippage_pct)),
    )
    names = sorted(set(symbols) | set(venue.held_symbols))
    venue.specs = await instrument_specs(names)

    complete_before = _hour_floor(now)  # bars with ts + 1h <= now
    replayed = venue.replayed_until or complete_before - HOUR
    # A retry on the same day when the exchange has already been replayed PAST the decision moment:
    # what was replayed is neither replayed again nor rolled back, the price mark is
    # at the decision moment, and the day's decision counts as made — no entries
    # (`decision_passed`). Deciding "as of 00:10" on an exchange that has seen 02:00
    # would mean looking into the future (third and fourth review rounds).
    venue.seen_until = max(replayed, venue.seen_until or replayed)
    venue.decision_passed = venue.seen_until > complete_before
    since = min(replayed, complete_before)
    events: list[str] = []
    for symbol in names:
        bars = [
            b for b in await load_bars(symbol, "1h", since - HOUR, complete_before)
            if b.ts >= since - HOUR
        ]
        held = symbol in venue.held_symbols
        # Exactly the last bar before the decision moment is needed. A two-hour
        # tolerance let the 23:00 bar slip: the "replayed until" mark moved to 00:00,
        # and a bar arriving later was never checked for the stop (sixth
        # review round, P-001). The entry price is also at the decision moment.
        fresh = bool(bars) and bars[-1].ts >= complete_before - HOUR
        if held and not fresh:
            last = f"{bars[-1].ts:%Y-%m-%d %H:%M}" if bars else "none"
            raise DataNotFresh(
                f"{symbol}: position is open, but the hourly bar before "
                f"{complete_before:%Y-%m-%d %H:%M} is missing (last: {last}) — unknown "
                "whether the stop triggered. Backfill first"
            )
        replayable = [b for b in bars if b.ts >= replayed]
        events += venue.replay(symbol, replayable)
        if not fresh:
            venue.mark.pop(symbol, None)  # an old price is not a price (NO_PRICE)
        elif bars:
            venue.mark[symbol] = bars[-1].close
    venue.replayed_until = max(replayed, complete_before)
    return venue, path, events


async def advance(venue: MarketPaperVenue, until: datetime) -> list[str]:
    """Replay of held positions over hourly bars up to `until` — after the decision.

    A position opened retroactively lives from the decision moment: its stop
    is checked against every bar from that moment. The "replayed until" mark
    moves only by what has been replayed FOR ALL held symbols:
    a data gap in one symbol must not count as a checked
    stop (P-001). The cycle accounts for a triggered stop on the next run —
    by the fill on the exchange, at its price.
    """
    target = _hour_floor(until)
    since = venue.replayed_until
    if since is None or since >= target:
        return []
    events: list[str] = []
    reached = target
    for symbol in venue.held_symbols:
        bars = [b for b in await load_bars(symbol, "1h", since, target) if b.ts >= since]
        events += venue.replay(symbol, bars)
        reached = min(reached, bars[-1].ts + HOUR) if bars else since
    venue.replayed_until = max(since, reached)
    return events


@dataclass
class MonitorResult:
    report: CycleReport | None
    events: list[str]
    #: Why the watch did nothing (None — it did something).
    idle: str | None = None


async def monitor_paper(
    settings: Settings, setup: str, *, now: datetime | None = None, store: Store | None = None
) -> MonitorResult:
    """Under the book lock (DEC-049)."""
    with book_lock(book_of(setup, "paper")):
        return await _monitor_paper(settings, setup, now=now, store=store)


async def _monitor_paper(
    settings: Settings, setup: str, *, now: datetime | None = None, store: Store | None = None
) -> MonitorResult:
    """Day watch over paper: stops — right away, decisions — none.

    The night cycle decides once a day. Between runs the paper exchange stood still:
    a stop triggered during the day counted only the next night, and the panel
    showed 00:00 prices. Here held positions are replayed over hourly
    bars up to the current hour and reconciled with accounting: triggered stops
    are counted, time exits (their moment is midnight) were already done
    at night, there are no entries — the candidate list is empty.

    Only after the day's decision is MADE (`decided_for`): replaying the
    exchange past midnight before the decision would take from the night cycle
    the right to decide "as of 00:10" — a rule derived over six review rounds.
    """
    now = now or datetime.now(UTC)
    store = store or DbStore()
    book = book_of(setup, "paper")
    moment = decision_moment(now)
    path = PAPER_DIR / f"{setup}.json"
    venue = MarketPaperVenue.load(
        path,
        fee_pct=Decimal(str(settings.costs.taker_fee_pct)),
        slippage_pct=Decimal(str(settings.costs.slippage_pct)),
    )
    if venue.decided_for is None or venue.decided_for < moment:
        return MonitorResult(None, [], "day's decision not made yet — waiting for the night cycle")
    state = await store.load_state(book)
    if state is not None and state.mode is TradingMode.OFF_MANUAL:
        # A halt waits for a human: reconciling every hour would mean halting
        # again every hour, with a journal block and a notification.
        return MonitorResult(None, [], "trading halted — waiting for a human (trade-resume)")
    # Open trades in ACCOUNTING, not just positions on the exchange: a stop that
    # the night replay has already filled but the cycle has not yet counted is a trade without
    # a position, and it must be counted now, not next night.
    if not venue.held_symbols and not await store.live_trades(book):
        return MonitorResult(None, [], "no open positions")
    venue.specs = await instrument_specs(venue.held_symbols)
    venue.autosave = path
    events = await advance(venue, now)
    params = SetupDParams.from_config(settings.strategy[setup])
    report = await run_cycle(
        CycleContext(
            setup=book, venue=venue, store=store,
            limits=GovernorLimits.from_config(settings.risk),
            sizing=Sizing.from_config(settings.risk),
            initial_equity=Decimal(str(settings.risk.equity_usd)),
            hold=timedelta(hours=params.time_stop_hours),
            candidates=[],
            correlation=CorrelationSource(settings.risk.correlation_lookback_days).between,
            funding_rates=funding_sum,
            now=now, dry_run=False,
        )
    )
    venue.save(path)
    return MonitorResult(report, events)


def book_of(setup: str, venue_kind: str) -> str:
    """The accounting key: each execution exchange has its own book.

    A shared book for paper and testnet would mean that reconciliation with testnet
    sees paper positions as phantoms — and vice versa.
    """
    return f"{setup}:{venue_kind}"


async def funding_sum(symbol: str, start: datetime, end: datetime) -> Decimal:
    schedule = await load_funding_schedule(symbol, start, end)
    return schedule.between(start, end)


async def run_daily(
    settings: Settings,
    setup: str,
    *,
    venue_kind: str,
    execute: bool,
    now: datetime | None = None,
    store: Store | None = None,
    gate: EntryGate | None = None,
    rehearse: bool = False,
    decide: bool = True,
) -> tuple[Prepared, CycleReport, list[str]]:
    """Under the book lock when executing (DEC-049). A preview without
    --execute writes nothing — and must not get in the way of a real run.

    `rehearse` is the Risk Manager's rehearsal on paper: the whole cycle,
    as in execution, but on a copy of the exchange (the file is not saved) and a copy
    of the book (`ReadOnlyStore`). A preview does not fit here: in it a time
    exit does not change equity, and on a day with closes the entry size would come out
    different from the Executor's — a false rejection by the second key.

    `decide=False` is an attempt without the day's decision, as when the universe
    is missing: exits and reconciliation run, but the decision is not counted
    and the exchange is not replayed forward. This is how the Executor works when
    the Risk Manager gave no valid answer: a retry an hour later decides on
    the honest state, not on an exchange that has already seen time past the decision
    (embedding review 2026-09-30).
    """
    if rehearse:
        if venue_kind != "paper":
            raise ValueError("rehearsal with execution — paper only")
        return await _run_daily(settings, setup, venue_kind=venue_kind, execute=True,
                                now=now, store=ReadOnlyStore(store or DbStore()),
                                persist=False)
    if not execute:
        return await _run_daily(settings, setup, venue_kind=venue_kind, execute=False,
                                now=now, store=store, gate=gate, decide=decide)
    with book_lock(book_of(setup, venue_kind)):
        return await _run_daily(settings, setup, venue_kind=venue_kind, execute=True,
                                now=now, store=store, gate=gate, decide=decide)


async def _run_daily(
    settings: Settings,
    setup: str,
    *,
    venue_kind: str,
    execute: bool,
    now: datetime | None = None,
    store: Store | None = None,
    gate: EntryGate | None = None,
    persist: bool = True,
    decide: bool = True,
) -> tuple[Prepared, CycleReport, list[str]]:
    now = now or datetime.now(UTC)
    # Saving the exchange to disk — only for real execution.
    persist = execute and persist
    store = store or DbStore()
    book = book_of(setup, venue_kind)
    prepared = await prepare(settings, setup, now)
    for symbol, skip in sorted(prepared.skips.items()):
        if skip is Skip.NO_SIGNAL:
            continue
        await store.journal(JournalEntry(ts=now, setup=book, action="SKIP", symbol=symbol,
                                         code=skip.value, dry_run=not execute))

    events: list[str] = []
    client = None
    live_clock = None
    as_of = now
    if venue_kind == "paper":
        as_of = paper_as_of(now)
        prepared.as_of = as_of
        venue, path, events = await paper_venue(settings, setup, prepared.universe, as_of)
        if persist and path is not None:
            # The exchange goes to disk BEFORE the cycle and after every order: accounting in the
            # database must not get ahead of the exchange file if the process crashes midway.
            venue.save(path)
            venue.autosave = path
        if venue.decision_passed:
            prepared.note = (
                f"paper has already seen prices after the decision moment (until "
                f"{(venue.seen_until or venue.replayed_until):%H:%M} UTC) — this day's entries "
                "are not repeated, "
                "only exits and reconciliation"
            )
            await store.journal(JournalEntry(
                ts=now, setup=book, action="DECISION_DONE", dry_run=not execute,
                note=prepared.note,
            ))
    elif venue_kind == "testnet":
        from src.core.config import get_env
        from src.exchange.trading_client import BybitTradingClient
        from src.execution.bybit_exchange import BybitExchange

        env = get_env()
        key, secret = env.bybit_api_key, env.bybit_api_secret
        if not key.get_secret_value() or not secret.get_secret_value():
            raise DataNotFresh("no testnet keys in .env: BYBIT_API_KEY / BYBIT_API_SECRET")
        client = BybitTradingClient(settings.bybit, api_key=key, api_secret=secret)
        venue, path = BybitExchange(client), None
        live_clock = lambda: datetime.now(UTC)  # noqa: E731 — entry window by the live clock
    else:
        raise ValueError(f"unknown execution exchange {venue_kind!r}")

    params = SetupDParams.from_config(settings.strategy[setup])
    mmr = {s: p[0] for s, p in (await load_instrument_params(prepared.universe)).items()}
    source = CorrelationSource(settings.risk.correlation_lookback_days)
    try:
        report = await run_cycle(
            CycleContext(
                setup=book, venue=venue, store=store,
                limits=GovernorLimits.from_config(settings.risk),
                sizing=Sizing.from_config(settings.risk),
                initial_equity=Decimal(str(settings.risk.equity_usd)),
                hold=timedelta(hours=params.time_stop_hours),
                candidates=(
                    [] if venue_kind == "paper" and venue.decision_passed
                    else prepared.candidates
                ),
                correlation=source.between,
                funding_rates=funding_sum,
                now=as_of, dry_run=not execute, mmr=mmr, clock=live_clock, gate=gate,
            )
        )
    finally:
        if client is not None:
            await client.aclose()
    # Replay forward — only when the day's decision is made. An attempt without a decision
    # (no universe, all data stale) leaves the exchange at the decision
    # moment: the next attempt decides on the honest state, and the stops
    # are caught up by the first attempt with a decision (or tomorrow's run).
    stale = [s for s, k in prepared.skips.items() if k is Skip.STALE_DATA]
    decided = decide and not prepared.problem and not (
        prepared.universe and len(stale) == len(prepared.universe)
    )
    if venue_kind == "paper" and decided and not report.halted:
        # The day's decision is made — from this moment the day watch may
        # replay the exchange forward. A cycle halted by reconciliation did not reach time
        # exits or entries: there is no decision (watch review).
        venue.decided_for = max(venue.decided_for or prepared.moment, prepared.moment)
    # A cycle halted by reconciliation — also no replay: there is no day's decision,
    # and after trade-resume the retry must decide "as of 00:10" (core review).
    if venue_kind == "paper" and as_of < now and decided and not report.halted:
        events += await advance(venue, now)
        await store.journal(JournalEntry(
            ts=now, setup=book, action="LATE_RUN", dry_run=not execute,
            note=f"run later than scheduled: decision on prices as of {as_of:%H:%M} UTC, "
                 f"stops replayed until {venue.replayed_until:%H:%M} UTC",
        ))
    if persist and path is not None:
        venue.save(path)
    return prepared, report, events


__all__ = [
    "LIVE_HISTORY_DAYS",
    "PAPER_DECISION_DELAY",
    "PAPER_DIR",
    "MonitorResult",
    "advance",
    "monitor_paper",
    "paper_as_of",
    "DataNotFresh",
    "book_of",
    "Prepared",
    "funding_sum",
    "month_of_decision",
    "instrument_specs",
    "paper_venue",
    "prepare",
    "run_daily",
    "universe_of",
]
