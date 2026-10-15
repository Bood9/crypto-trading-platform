"""System CLI.

Phase 1a: tradability report. Collectors and backfill arrive in Phase 1b.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from src.collectors.base import CollectResult
from src.collectors.checkpoints import find_stale
from src.collectors.concurrency import bounded
from src.collectors.coverage import (
    build_availability,
    estimate_backfill,
    summarize_gaps,
)
from src.collectors.funding import FundingCollector
from src.collectors.ohlcv import TIMEFRAMES, OhlcvCollector
from src.collectors.open_interest import OI_INTERVALS, OpenInterestCollector
from src.collectors.repair import repair_symbol, verify_repair
from src.collectors.stream_starts import (
    assess_readiness,
    collect_starts,
    compute_coverage,
    persist_starts,
)
from src.collectors.validation import (
    duplicate_keys,
    summarize_bar_gaps,
    validate_ohlcv,
)
from src.core.config import get_env, get_settings
from src.core.db import dispose_engine
from src.core.logging import configure_logging, get_logger
from src.core.models import DataStream
from src.exchange.bybit_client import BybitCircuitOpen, BybitClient
from src.exchange.instrument_filter import InstrumentFilter
from src.exchange.repository import upsert_instruments
from src.report.status import Block
from src.strategy.universe_builder import build_all_months

#: Regular step of each stream — the basis for watchdog thresholds.
#: funding uses the most frequent interval (60 min): the threshold must
#: fire on the most demanding symbol, otherwise rare streams would
#: mask a failure on frequent ones.
STREAM_INTERVALS: dict[DataStream, timedelta] = {
    DataStream.KLINE_15M: TIMEFRAMES["15m"][1],
    DataStream.KLINE_1H: TIMEFRAMES["1h"][1],
    DataStream.KLINE_4H: TIMEFRAMES["4h"][1],
    DataStream.OI: OI_INTERVALS["1h"],
    DataStream.FUNDING: timedelta(hours=8),
}

#: Exit codes of bulk loading (P-002).
EXIT_FAILED = 1
#: Some streams failed to load. Neither a silent success nor a total failure:
#: whether the gap is tolerable is up to the caller.
#:
#: Exactly 3, not 2: Typer uses 2 to report a usage error. A script that
#: tolerates partial failure would then also tolerate a typo in arguments —
#: the very kind of silent success P-002 guards against.
EXIT_PARTIAL = 3

app = typer.Typer(add_completion=False, help="Multi-agent trading system, Bybit")
# Width is set explicitly: under a pipe rich assumes an 80-column terminal
# and truncates symbols with an ellipsis, while the report is read by eye first.
console = Console(width=118)
log = get_logger(__name__)


@app.callback()
def main() -> None:
    """Command group.

    Needed so Typer does not collapse a single command into the root:
    otherwise `universe-report` would be parsed as an extra positional argument,
    and with the arrival of Phase 1b commands the CLI behaviour would silently change.
    """


def _fmt(value: Decimal, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


@app.command("universe-report")
def universe_report(
    limit: int = typer.Option(40, help="How many rows to show"),
    tradable_only: bool = typer.Option(False, help="Tradable only"),
    export: Path | None = typer.Option(None, help="Export the full report to JSON"),
    save: bool = typer.Option(True, help="Save instrument parameters to the DB"),
) -> None:
    """Which coins we can trade with the current deposit.

    Answers "is this feasible at all" before any strategy.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        async with BybitClient(settings.bybit, testnet=env.bybit_testnet) as client:
            report, specs = await InstrumentFilter(client, settings).build_report()

        if save:
            # The DB may be down — the report does not lose its meaning,
            # so we must not fail here.
            try:
                saved = await upsert_instruments(list(specs.values()))
                console.print(f"Instruments saved to DB: [bold]{saved}[/bold]")
            except Exception as exc:
                console.print(f"[yellow]DB unavailable, report not saved: {exc}[/yellow]")
            finally:
                await dispose_engine()

        risk = settings.risk
        margin = Decimal(str(risk.equity_usd)) * Decimal(str(risk.margin_per_trade_pct)) / 100
        target = margin * Decimal(str(risk.leverage_cap))

        console.print(
            f"\n[bold]Deposit[/bold] ${risk.equity_usd:.2f}  ·  "
            f"[bold]margin per trade[/bold] ${margin:.2f} "
            f"({risk.margin_per_trade_pct}%)  ·  "
            f"[bold]leverage cap[/bold] {risk.leverage_cap}x  ·  "
            f"[bold]target notional[/bold] ${target:.2f}\n"
        )

        rows = [r for r in report if r.tradable] if tradable_only else report

        table = Table(show_lines=False, header_style="bold")
        table.add_column("#", justify="right", style="dim")
        table.add_column("symbol")
        table.add_column("turnover 24h", justify="right")
        table.add_column("price", justify="right")
        table.add_column("min. entry", justify="right")
        table.add_column("step", justify="right")
        table.add_column("parts", justify="right")
        table.add_column("MMR", justify="right")
        table.add_column("verdict")

        for i, r in enumerate(rows[:limit], 1):
            verdict = (
                "[green]OK[/green]"
                if r.tradable
                else f"[red]{r.rejection.value if r.rejection else 'NO'}[/red]"
            )

            table.add_row(
                str(i),
                r.symbol,
                f"{r.turnover_24h_usd / 1_000_000:,.0f}M",
                _fmt(r.price, 6).rstrip("0").rstrip("."),
                f"${_fmt(r.effective_min_notional)}" if r.effective_min_notional else "—",
                f"${_fmt(r.granularity_usd, 4)}" if r.granularity_usd else "—",
                str(r.max_parts) if r.max_parts else "—",
                f"{r.mmr * 100:.2f}%" if r.mmr is not None else "—",
                verdict,
            )

        console.print(table)

        tradable = [r for r in report if r.tradable]
        console.print(
            f"\nTotal considered: [bold]{len(report)}[/bold]  ·  "
            f"tradable: [bold green]{len(tradable)}[/bold green]  "
            f"(split threshold: {settings.universe.min_position_parts} parts)\n"
        )

        blocked = [r for r in report if not r.tradable]
        if blocked:
            by_reason: dict[str, int] = {}
            for r in blocked:
                key = r.rejection.value if r.rejection else "UNKNOWN"
                by_reason[key] = by_reason.get(key, 0) + 1
            summary = "  ".join(f"{k} ×{v}" for k, v in sorted(by_reason.items()))
            console.print(f"Rejected: {summary}\n")

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(
                    [json.loads(r.model_dump_json()) for r in report],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            console.print(f"Full report exported: [bold]{export}[/bold]\n")

    asyncio.run(run())


@app.command("backfill")
def backfill(
    symbols: str = typer.Option(..., help="Comma-separated symbols"),
    years: float = typer.Option(2.0, help="History depth in years"),
    streams: str = typer.Option("all", help="kline_15m,kline_1h,kline_4h,oi,funding or all"),
    full: bool = typer.Option(False, help="Ignore the checkpoint and download from scratch"),
    estimate_only: bool = typer.Option(
        False, "--estimate", help="Only estimate volume and time, without loading"
    ),
    concurrency: int = typer.Option(
        1,
        help="How many symbols to download concurrently. The rate is still "
        "held by the client's token bucket, so the limit cannot be exceeded",
    ),
) -> None:
    """Load historical data.

    Incremental by default: a rerun fetches what is missing
    from the checkpoint rather than re-downloading everything.

    Before a bulk run it is worth running with --estimate: this is the
    longest operation of the phase, and learning its cost halfway is unpleasant.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    requested = (
        set(settings.data.streams)
        if streams == "all"
        else {s.strip() for s in streams.split(",") if s.strip()}
    )
    unknown = requested - set(settings.data.streams)
    if unknown:
        # P-001: a typo in a stream name must not silently narrow the load.
        raise typer.BadParameter(f"unknown streams: {sorted(unknown)}")

    # Same parsing as other commands: a string, a file or
    # 'universe'. Previously only comma parsing was here, and a file path
    # went to the exchange as a symbol name — Bybit answered "Symbol
    # Invalid" for every stream, and the pipeline kept going (DEC-026).
    async def resolve() -> list[str]:
        # DB connections are bound to the event loop. This loop
        # closes right here, and loading runs in the next one — without disposing
        # the engine it would get connections of a dead loop and fail
        # ("attached to a different loop") on any list from the DB.
        try:
            return await _resolve_symbols(symbols)
        finally:
            await dispose_engine()

    symbol_list = asyncio.run(resolve())
    bogus = [s for s in symbol_list if not s.replace("_", "").isalnum()]
    if bogus:
        # P-001: a name with paths and dots is not a symbol but a usage error.
        raise typer.BadParameter(
            f"these are not symbol names: {bogus[:3]} — was a path "
            "to a nonexistent file passed?"
        )
    if not symbol_list and symbols == "held":
        console.print("no open positions — nothing to collect")
        return
    if not symbol_list:
        raise typer.BadParameter("symbol list is empty")
    start = datetime.now(UTC) - timedelta(days=int(years * 365.25))

    est = estimate_backfill(
        len(symbol_list),
        [DataStream(s) for s in sorted(requested)],
        years,
        requests_per_second=settings.bybit.rate_limit.requests_per_second,
    )
    console.print(
        f"\n[bold]Estimate[/bold]: {est.symbols} symbols × {est.streams} streams "
        f"× {years:g} years\n"
        f"  requests:  ~{est.requests:,}\n"
        f"  rows:      ~{est.rows:,}\n"
        f"  on disk:   ~{est.gigabytes:.2f} GB before compression\n"
        f"  time:      ~{est.human_time()} "
        f"at {settings.bybit.rate_limit.requests_per_second:g} req/s\n"
        f"[dim]Upper-bound estimate: assumes full coverage for every "
        f"symbol. The real volume is smaller — some pairs are younger than the window.[/dim]\n"
    )
    if estimate_only:
        return

    async def run() -> None:
        started = time.monotonic()
        circuit_tripped = False
        async with BybitClient(settings.bybit, testnet=env.bybit_testnet) as client:
            specs = await InstrumentFilter(client, settings).fetch_specs()
            # Specs are already downloaded — save them: the exchange changes price
            # and lot steps without warning, and the night cycle reads them from cache.
            # A write failure must not break candle collection.
            try:
                await upsert_instruments(list(specs.values()))
            except Exception as exc:  # noqa: BLE001
                console.print(f"[yellow]instrument cache not updated: {exc}[/yellow]")
            results: list[CollectResult] = []

            async def one_symbol(symbol: str) -> None:
                nonlocal circuit_tripped
                spec = specs.get(symbol)
                # The symbol may be delisted: it is absent from instruments-info,
                # but the exchange still has its history. The funding interval is
                # then unknown, so we take the standard 8 hours.
                funding_interval = spec.funding_interval_minutes if spec else 480
                if spec is None:
                    console.print(
                        f"[yellow]{symbol}: not in instruments-info "
                        f"(delisted?), funding interval assumed 480 min[/yellow]"
                    )

                for stream in sorted(requested):
                    if circuit_tripped:
                        return
                    collector = _make_collector(client, stream, funding_interval)
                    try:
                        res = await collector.run(
                            symbol, start=start, incremental=not full
                        )
                    except BybitCircuitOpen as exc:
                        if not circuit_tripped:
                            console.print(
                                f"\n[bold red]CIRCUIT BREAKER TRIPPED[/bold red]\n{exc}\n"
                                "[yellow]Run aborted. Progress is saved in checkpoints: "
                                "once the cause is fixed, a rerun will continue "
                                "from the point of interruption.[/yellow]"
                            )
                        circuit_tripped = True
                        return
                    results.append(res)
                    status = "[red]ERROR[/red]" if not res.ok else f"{res.rows_written} rows"
                    console.print(
                        f"  {symbol:<14} {stream:<10} {status}"
                        + (f"  [dim]{res.error}[/dim]" if res.error else "")
                    )
                done_symbols.append(symbol)
                console.print(
                    f"[dim]  symbols done: {len(done_symbols)}/{len(symbol_list)}[/dim]"
                )

            # Symbols are parallelised, not streams within a symbol: checkpoints
            # of a (symbol, stream) pair are independent, there is nothing
            # to share between symbols. The total rate is held by the client's token bucket.
            done_symbols: list[str] = []
            await bounded(
                symbol_list,
                one_symbol,
                limit=concurrency,
                stop=lambda: circuit_tripped,
            )

        await dispose_engine()

        elapsed = time.monotonic() - started
        total = sum(r.rows_written for r in results)
        requests = sum(r.requests for r in results)
        failed = [r for r in results if not r.ok]

        console.print(
            f"\nRows written: [bold]{total:,}[/bold]  ·  "
            f"requests: [bold]{requests:,}[/bold]  ·  "
            f"time: [bold]{elapsed:.1f}s[/bold]"
        )
        if elapsed > 0 and symbol_list:
            per_symbol = elapsed / len(symbol_list)
            console.print(
                f"Actual rate: [bold]{requests / elapsed:.1f}[/bold] req/s  ·  "
                f"[bold]{per_symbol:.1f}s[/bold] per symbol  ·  "
                f"{total / elapsed:,.0f} rows/s"
            )
            console.print(
                f"[dim]Extrapolation: 100 symbols ≈ "
                f"{per_symbol * 100 / 60:.0f} min, "
                f"513 symbols ≈ {per_symbol * 513 / 3600:.1f} h[/dim]"
            )
        if failed:
            console.print(f"[red]Failed streams: {len(failed)}[/red]")

        # P-002: the result is reported by the exit code, not by text.
        # Previously this was always zero — "ERROR" was printed on every stream,
        # while the pipeline kept going and rebuilt the universe
        # on incomplete data (DEC-026).
        #
        # Three outcomes, because they have different costs. A partial failure
        # on bulk loading is expected — some delisted pairs lack
        # one stream or another — and the caller decides whether it is tolerable.
        # A total failure and a tripped circuit breaker are never tolerable.
        if circuit_tripped or (results and len(failed) == len(results)):
            raise typer.Exit(code=EXIT_FAILED)
        if not results:
            raise typer.Exit(code=EXIT_FAILED)
        if failed:
            raise typer.Exit(code=EXIT_PARTIAL)

    asyncio.run(run())


#: Exit code of `build-universe --check`: time to build the month.
EXIT_UNIVERSE_DUE = 5


async def _universe_months() -> set[str]:
    """Months that have at least one member. A month without members is
    not built but failed: it must be built again."""
    from sqlalchemy import select as _select

    from src.core.db import UniverseSnapshot, session_scope

    async with session_scope() as s:
        return set(
            (
                await s.execute(
                    _select(UniverseSnapshot.month)
                    .where(UniverseSnapshot.included.is_(True))
                    .distinct()
                )
            ).scalars()
        )


#: Share of tradable symbols with data in the selection month whose candles
#: reached its end. Below it, candidate collection failed, and selection by
#: incomplete turnover would be skewed towards those already refreshed.
MIN_SELECTION_FRESH_SHARE = 0.95


def is_universe_candidate(symbol_type: str | None, base_coin: str | None, universe) -> bool:  # noqa: ANN001
    """The same "crypto only" verdict as the instrument filter (DEC-002) —
    via the same function, not a copy of it."""
    from types import SimpleNamespace

    from src.exchange.instrument_filter import is_crypto

    if not universe.crypto_only:
        return True
    spec = SimpleNamespace(symbol_type=symbol_type or "", base_coin=base_coin or "")
    return is_crypto(
        spec,
        set(universe.allowed_symbol_types),
        {c.upper() for c in universe.excluded_base_coins},
    )


def selection_gap(selection: str, last_bars: dict[str, datetime | None]) -> str | None:
    """Why the selection month is not fully collected, or None.

    `last_bars` — EVERY tradable symbol that should have been trading
    in the selection month, and its last 4h candle inside the month (None — no
    candles at all). A symbol without candles is lagging, not absent: otherwise
    a failed candidate collection would make the denominator smaller and the share
    of fresh ones higher (third review round 2026-09-28).
    """
    from src.strategy.universe import month_bounds

    if len(last_bars) < 30:
        return f"in selection month {selection} only {len(last_bars)} symbols should trade"
    last_bar = month_bounds(selection)[1] - timedelta(hours=4)
    fresh = sum(1 for ts in last_bars.values() if ts is not None and ts >= last_bar)
    share = fresh / len(last_bars)
    if share >= MIN_SELECTION_FRESH_SHARE:
        return None
    stale = sorted(sym for sym, ts in last_bars.items() if ts is None or ts < last_bar)
    return (
        f"selection month {selection} is fully collected for only {fresh} of {len(last_bars)} "
        f"symbols ({share:.0%}); lagging: {', '.join(stale[:10])}"
        + (" …" if len(stale) > 10 else "")
        + ". Run backfill --symbols candidates first"
    )


async def _selection_month_gap(month: str, tradable: frozenset[str], settings) -> str | None:  # noqa: ANN001
    """Why the selection month for `month` is not fully collected, or None.

    Looks at EVERY tradable candidate, not at the freshest candle
    in the DB: that one is always fresh — the night collection refreshes
    the current month's universe first (fixes review 2026-09-28).
    """
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from src.core.db import Instrument, Ohlcv, session_scope
    from src.strategy.universe import month_bounds, previous_month

    selection = previous_month(month, settings.universe.selection_lag_months)
    begin, end = month_bounds(selection)
    names = sorted(tradable)
    async with session_scope() as s:
        inside = dict(
            (
                await s.execute(
                    _select(Ohlcv.symbol, _func.max(Ohlcv.ts))
                    .where(
                        Ohlcv.timeframe == "4h",
                        Ohlcv.symbol.in_(names),
                        Ohlcv.ts >= begin,
                        Ohlcv.ts < end,
                    )
                    .group_by(Ohlcv.symbol)
                )
            ).all()
        )
        # Should have been trading: listed before the end of the selection month
        # (with a one-day margin — a fresh listing has nothing to collect) or
        # candles up to its end already exist.
        listed = set(
            (
                await s.execute(
                    _select(Instrument.symbol).where(
                        Instrument.symbol.in_(names),
                        Instrument.launch_time < end - timedelta(days=1),
                    )
                )
            ).scalars()
        )
        seen = set(
            (
                await s.execute(
                    _select(Ohlcv.symbol)
                    .where(Ohlcv.timeframe == "4h", Ohlcv.symbol.in_(names), Ohlcv.ts < end)
                    .distinct()
                )
            ).scalars()
        )
    expected = (listed | seen | set(inside)) & set(names)
    return selection_gap(selection, {sym: inside.get(sym) for sym in expected})


def due_universe_months(now: datetime, existing: set[str]) -> tuple[list[str], list[str]]:
    """Which universe months the nearest decisions need: (can build, too early).

    Today's decision and tomorrow's decision — their months (by the signal bar,
    as in the backtest). A month can be built once its selection month has ended,
    i.e. its first day has arrived.
    """
    from src.live.runner import month_of_decision
    from src.live.signals import decision_moment
    from src.strategy.universe import month_bounds

    moment = decision_moment(now)
    needed = {month_of_decision(moment), month_of_decision(moment + timedelta(days=1))}
    missing = sorted(needed - existing)
    ready = [m for m in missing if now >= month_bounds(m)[0]]
    return ready, [m for m in missing if m not in ready]


@app.command("build-universe")
def build_universe_cmd(
    years: float = typer.Option(2.0, help="Period depth in years"),
    show: int = typer.Option(0, help="Show the composition of the last N months"),
    export: Path | None = typer.Option(None, help="Export the symbol list"),
    month: str = typer.Option(
        "",
        help="Build ONE month: YYYY-MM or due — the one needed by the nearest decisions. "
        "Past months are not recomputed",
    ),
    check: bool = typer.Option(
        False, "--check", help="With --month due: only report whether it is time (code 5 — due)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="With --month: build and show, but do not save"
    ),
) -> None:
    """Build a point-in-time universe from the collected 4h candles.

    Chicken and egg: selecting symbols requires their turnover, and turnover
    lives in candles. Solved by ordering — first the light 4h for all
    candidates, then the full depth only for the selected ones.

    Without `--month` ALL months of the period are recomputed — past ones too,
    with today's tradability verdict. For the monthly step this is
    wrong: the snapshot of a past month that was already traded must not
    change. Use `--month due` for that.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    if month and month != "due":
        try:
            datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise typer.BadParameter("month: YYYY-MM or due") from exc
    if check and month != "due":
        raise typer.BadParameter("--check only together with --month due")
    if dry_run and not month:
        raise typer.BadParameter("--dry-run only together with --month")

    async def run() -> None:
        from src.strategy.universe import month_bounds

        # The period is tied to the data, not to the calendar. With `now()` the set
        # of months drifts every day, and rebuilding the snapshot gives a
        # different composition — the same reproducibility violation as
        # with a live price (DEC-015).
        end = await _data_end(None, timeframe="4h")
        start = end - timedelta(days=int(years * 365.25))
        targets: list[str] = []
        if month:
            if month == "due":
                ready, early = due_universe_months(datetime.now(UTC), await _universe_months())
                for m in early:
                    console.print(f"universe {m}: too early — selection month has not ended yet")
                if not ready:
                    console.print("universe: nothing to build")
                    await dispose_engine()
                    return
                if check:
                    console.print(f"universe: time to build {', '.join(ready)}")
                    await dispose_engine()
                    raise typer.Exit(code=EXIT_UNIVERSE_DUE)
                targets = ready
            else:
                targets = [month]

        # The tradability verdict comes from the Instrument Filter on live
        # prices: the instruments table has no price, and deriving it
        # from min_notional would be guessing.
        async with BybitClient(settings.bybit, testnet=env.bybit_testnet) as client:
            report, _ = await InstrumentFilter(client, settings).build_report()
        tradable = frozenset(r.symbol for r in report if r.tradable)
        known = frozenset(r.symbol for r in report)

        # Delisted: absent from instruments-info, hence no lot step
        # and no price. There is nothing to check tradability with — but rejecting
        # is not allowed either, or survivorship bias returns (DEC-006). Include
        # with a flag; the share of trades on them goes into the backtest report.
        from src.strategy.universe_builder import symbols_with_data

        unverifiable = frozenset(await symbols_with_data()) - known
        if targets:
            # For a FUTURE month there are no "unverifiable" ones: one can trade
            # only what trades now. Otherwise everything absent from the filter
            # report — stocks, ETFs, commodities — would pass as "delisted"
            # (fixes review 2026-09-28).
            unverifiable = frozenset()

        console.print(
            f"Tradable per Instrument Filter: [bold]{len(tradable)}[/bold]  ·  "
            f"unverifiable (delisted): [bold yellow]{len(unverifiable)}[/bold yellow]"
        )

        if targets:
            # Exactly these months: period start and end are the month's first day.
            from src.strategy.universe_builder import UniverseBuildResult, persist_entries

            result = UniverseBuildResult(entries=[], months=[])
            for m in targets:
                problem = await _selection_month_gap(m, tradable, settings)
                if problem:
                    await dispose_engine()
                    console.print(f"[bold red]{m}: {problem}[/bold red]")
                    raise typer.Exit(code=EXIT_FAILED)
                first = month_bounds(m)[0]
                one = await build_all_months(
                    first, first, settings.universe,
                    tradable=tradable, unverifiable=unverifiable, persist=False,
                )
                included = sum(1 for e in one.entries if e.included)
                if not included:
                    # Do not save: a saved empty month would count as
                    # built, and there would be no retry.
                    await dispose_engine()
                    console.print(
                        f"[bold red]{m}: nobody made it into the universe — not saved[/bold red]"
                    )
                    raise typer.Exit(code=EXIT_FAILED)
                if not dry_run:
                    await persist_entries(one.entries)
                result.entries.extend(one.entries)
                result.months.extend(one.months)
                console.print(
                    f"universe {m}: built, {included} symbols in the universe"
                    + (" (NOT saved: --dry-run)" if dry_run else "")
                )
        else:
            result = await build_all_months(
                start,
                end,
                settings.universe,
                tradable=tradable,
                unverifiable=unverifiable,
            )
        symbols = result.selected_symbols()
        counts = result.months_by_symbol()

        console.print(
            f"\nMonths in period: [bold]{len(result.months)}[/bold]  ·  "
            f"snapshot rows: [bold]{len(result.entries):,}[/bold]  ·  "
            f"unique symbols in the universe: [bold green]{len(symbols)}[/bold green]"
        )

        by_reason: dict[str, int] = {}
        for e in result.entries:
            if not e.included:
                key = e.rejection.value if e.rejection else "UNKNOWN"
                by_reason[key] = by_reason.get(key, 0) + 1
        if by_reason:
            summary = "  ".join(f"{k} ×{v}" for k, v in sorted(by_reason.items()))
            console.print(f"Rejections (symbol-month): {summary}")

        table = Table(header_style="bold", title="Most persistent members")
        table.add_column("symbol")
        table.add_column("months in universe", justify="right")
        for symbol, n in sorted(counts.items(), key=lambda kv: -kv[1])[:15]:
            table.add_row(symbol, f"{n} / {len(result.months)}")
        console.print(table)

        if show:
            for shown in result.months[-show:]:
                members = sorted(
                    (e for e in result.entries if e.month == shown and e.included),
                    key=lambda e: e.rank,
                )
                names = ", ".join(e.symbol for e in members)
                console.print(f"\n[bold]{shown}[/bold] ({len(members)}): {names}")

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text("\n".join(symbols), encoding="utf-8")
            console.print(f"\nSymbol list exported: [bold]{export}[/bold]")

        await dispose_engine()

    asyncio.run(run())


@app.command("coverage-report")
def coverage_report(
    symbols: str = typer.Option("universe", help="Symbols, file or 'universe'"),
    years: float = typer.Option(2.0, help="Window depth in years"),
    streams: str = typer.Option("all", help="Comma-separated streams or all"),
    show_all: bool = typer.Option(False, help="Show all gaps, not just the summary"),
    export: Path | None = typer.Option(None, help="Export the matrix to JSON"),
) -> None:
    """Gap report per (symbol, month, stream) triple.

    Fills the availability matrix. Coverage cannot be computed per symbol:
    MULTIUSDT returns candles but not OI and funding — this is a class
    of cases, not an exception.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    requested = (
        set(settings.data.streams)
        if streams == "all"
        else {s.strip() for s in streams.split(",") if s.strip()}
    )
    unknown = requested - set(settings.data.streams)
    if unknown:
        raise typer.BadParameter(f"unknown streams: {sorted(unknown)}")

    stream_list = [DataStream(s) for s in sorted(requested)]
    end = datetime.now(UTC)
    start = end - timedelta(days=int(years * 365.25))

    async def run() -> None:
        symbol_list = await _resolve_symbols(symbols)
        async with BybitClient(settings.bybit, testnet=env.bybit_testnet) as client:
            specs = await InstrumentFilter(client, settings).fetch_specs()

        all_gaps = []
        matrix: list[dict] = []
        for symbol in symbol_list:
            spec = specs.get(symbol)
            gaps = await build_availability(
                symbol,
                stream_list,
                start,
                end,
                funding_interval_minutes=spec.funding_interval_minutes if spec else 480,
            )
            all_gaps.extend(gaps)
            matrix.extend(
                {
                    "symbol": g.symbol,
                    "month": g.month,
                    "stream": g.stream.value,
                    "actual": g.actual,
                    "expected": g.expected,
                    "completeness": round(g.completeness, 4),
                    "absent": g.absent,
                }
                for g in gaps
            )

            by_stream: dict[str, list] = {}
            for g in gaps:
                by_stream.setdefault(g.stream.value, []).append(g)

            if not gaps:
                console.print(f"[green]{symbol}[/green]: full coverage")
                continue

            parts = []
            for stream_name, items in sorted(by_stream.items()):
                absent = sum(1 for g in items if g.absent)
                mark = f"{stream_name}: {len(items)} mo"
                if absent:
                    mark += f" ([red]{absent} empty[/red])"
                parts.append(mark)
            console.print(f"[yellow]{symbol}[/yellow]: " + ", ".join(parts))

            if show_all:
                for g in sorted(gaps, key=lambda x: (x.stream.value, x.month)):
                    state = "EMPTY" if g.absent else f"{g.completeness:.0%}"
                    console.print(
                        f"    {g.month}  {g.stream.value:<10} "
                        f"{g.actual:>6}/{g.expected:<6} {state}"
                    )

        await dispose_engine()

        summary = summarize_gaps(all_gaps)
        console.print(
            f"\nTotal gaps: [bold]{summary['total']}[/bold]  ·  "
            f"months fully empty: [bold]{summary['absent']}[/bold]  ·  "
            f"partial: [bold]{summary['partial']}[/bold]"
        )
        if summary["absent"]:
            console.print(
                "[dim]An empty month for a stream is not a collection failure. The symbol may "
                "be younger than the window, or the stream may be missing entirely "
                "(the MULTIUSDT case).[/dim]"
            )

        # Heat map: share of covered months per stream.
        by_stream_month: dict[str, dict[str, int]] = {}
        for g in all_gaps:
            by_stream_month.setdefault(g.stream.value, {})
            by_stream_month[g.stream.value][g.month] = (
                by_stream_month[g.stream.value].get(g.month, 0) + 1
            )
        if by_stream_month:
            months = sorted({m for v in by_stream_month.values() for m in v})
            table = Table(header_style="bold", title="Gaps by month and stream")
            table.add_column("stream")
            for m in months:
                table.add_column(m[2:], justify="right")
            for stream_name in sorted(by_stream_month):
                row = [stream_name]
                for m in months:
                    n = by_stream_month[stream_name].get(m, 0)
                    row.append(f"[red]{n}[/red]" if n > 20 else (str(n) if n else "·"))
                table.add_row(*row)
            console.print(table)

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(matrix, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            console.print(f"Matrix exported: [bold]{export}[/bold]")

    asyncio.run(run())


@app.command("validate")
def validate_cmd(
    symbols: str = typer.Option("universe", help="Comma-separated symbols, file or 'universe'"),
    timeframe: str = typer.Option("4h", help="Timeframe: 15m, 1h, 4h"),
    show_worst: int = typer.Option(15, help="How many worst symbols to show"),
    export: Path | None = typer.Option(None, help="Export the report to JSON"),
) -> None:
    """Integrity check of the collected series.

    A matching row count does not mean integrity. Checked are
    timestamp monotonicity, duplicates, placeholder bars with zero volume
    and breaks longer than one interval.
    """
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        names = await _resolve_symbols(symbols)
        console.print(f"Checking [bold]{len(names)}[/bold] symbols, timeframe {timeframe}\n")

        reports = []
        for name in names:
            reports.append(await validate_ohlcv(name, timeframe))

        dupes = await duplicate_keys(timeframe)
        present = [r for r in reports if r.rows > 0]
        dirty = [r for r in present if not r.clean]

        console.print(
            f"With data: [bold]{len(present)}[/bold] of {len(reports)}  ·  "
            f"clean: [bold green]{len(present) - len(dirty)}[/bold green]  ·  "
            f"with remarks: [bold yellow]{len(dirty)}[/bold yellow]"
        )
        console.print(f"Key duplicates in the whole table: [bold]{dupes}[/bold]")

        order_broken = sum(r.out_of_order for r in present)
        zero_bars = sum(r.zero_volume_bars for r in present)
        console.print(
            f"Order violations: [bold]{order_broken}[/bold]  ·  "
            f"zero-volume bars: [bold]{zero_bars}[/bold]"
        )

        dist = summarize_bar_gaps(present, top=show_worst)
        console.print(
            f"\n[bold]Gaps[/bold]\n"
            f"  symbols with gaps:           {dist.symbols_with_gaps} of {dist.symbols} "
            f"({dist.share_with_gaps:.0%})\n"
            f"  bars missing:                {dist.total_missing:,} of "
            f"{dist.total_expected:,} expected ({dist.overall_gap_share:.3%})\n"
            f"  per symbol-month, median:    "
            f"{dist.median_per_symbol_month:.1f}\n"
            f"  per symbol-month, 95th pct:  "
            f"{dist.p95_per_symbol_month:.1f}\n"
            f"  max contiguous gap:          "
            f"{dist.longest_gap_overall} bars"
        )

        if dist.suspicious_regularity:
            console.print(
                f"\n[bold red]P-004: suspiciously regular gaps[/bold red] "
                f"in {len(dist.suspicious_regularity)} symbols of "
                f"{dist.symbols_with_gaps} with gaps"
            )
            table = Table(header_style="bold red")
            table.add_column("symbol")
            table.add_column("CV", justify="right")
            table.add_column("period, bars", justify="right")
            for name, cv, period in dist.suspicious_regularity[:10]:
                table.add_row(name, f"{cv:.3f}", str(period))
            console.print(table)
            console.print(
                "[yellow]Regularity where randomness is expected is "
                "a sign of a collection defect, not a market property.[/yellow]"
            )
        elif dist.symbols_with_gaps:
            console.print(
                "\n[green]P-004: gaps are irregular[/green] — "
                "looks like market gaps, not a collection defect"
            )

        if dist.worst:
            table = Table(header_style="bold", title="Worst by missing share")
            table.add_column("symbol")
            table.add_column("missing share", justify="right")
            table.add_column("max gap, bars", justify="right")
            for name, share, longest in dist.worst:
                table.add_row(name, f"{share:.2%}", str(longest))
            console.print(table)

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(
                    [
                        {
                            "symbol": r.symbol,
                            "timeframe": r.timeframe,
                            "rows": r.rows,
                            "first_ts": r.first_ts.isoformat() if r.first_ts else None,
                            "last_ts": r.last_ts.isoformat() if r.last_ts else None,
                            "duplicates": r.duplicates,
                            "out_of_order": r.out_of_order,
                            "zero_volume_bars": r.zero_volume_bars,
                            "gap_count": len(r.gaps),
                            "missing_bars": r.missing_bars,
                            "gap_share": r.gap_share,
                            "longest_gap": r.longest_gap,
                        }
                        for r in reports
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            console.print(f"\nReport exported: [bold]{export}[/bold]")

        await dispose_engine()

    asyncio.run(run())


async def _resolve_symbols(spec: str) -> list[str]:
    """Symbol list from a string, a file or the universe.

    'universe' — everyone who was ever included; 'universe-now' — the universe
    of the current month plus open positions of the live cycle: exactly what
    needs refreshing before the daily decision.
    """
    if spec == "candidates":
        # Universe candidates — by THE SAME rules as the instrument
        # filter: quote coin, contract type, trading, crypto only
        # (DEC-002). Without the last one 258 stocks, ETFs and commodities came here
        # (fixes review 2026-09-28). From the cache refreshed by the night collection.
        from sqlalchemy import select as _select

        from src.core.db import Instrument, session_scope

        universe = get_settings().universe
        async with session_scope() as session:
            rows = (
                await session.execute(
                    _select(Instrument.symbol, Instrument.symbol_type, Instrument.base_coin)
                    .where(
                        Instrument.status == "Trading",
                        Instrument.quote_coin == universe.quote_coin,
                        Instrument.contract_type == universe.contract_type,
                    )
                )
            ).all()
        return sorted(
            symbol for symbol, symbol_type, base in rows
            if is_universe_candidate(symbol_type, base, universe)
        )
    if spec == "held":
        # Open positions of the live cycle — for the day watch.
        from sqlalchemy import select as _select

        from src.core.db import LivePosition, session_scope

        async with session_scope() as session:
            held = (
                await session.execute(
                    _select(LivePosition.symbol)
                    .where(LivePosition.status.in_(("pending", "open")))
                    .distinct()
                )
            ).scalars().all()
        return sorted(held)
    if spec == "universe-now":
        from sqlalchemy import select as _select

        from src.core.db import LivePosition, session_scope
        from src.live.runner import month_of_decision, universe_of
        from src.live.signals import decision_moment

        # The month of today's decision and of tomorrow's: on the first day
        # they differ, and both lists need refreshing.
        now = datetime.now(UTC)
        months = {
            month_of_decision(decision_moment(now)),
            month_of_decision(decision_moment(now) + timedelta(days=1)),
        }
        names = set()
        for month in sorted(months):
            names |= set(await universe_of(month))
        async with session_scope() as session:
            held = (
                await session.execute(
                    _select(LivePosition.symbol).where(
                        LivePosition.status.in_(("pending", "open"))
                    )
                )
            ).scalars().all()
        return sorted(names | set(held))
    if spec == "universe":
        from sqlalchemy import select as _select

        from src.core.db import UniverseSnapshot, session_scope

        async with session_scope() as session:
            rows = (
                await session.execute(
                    _select(UniverseSnapshot.symbol)
                    .where(UniverseSnapshot.included.is_(True))
                    .distinct()
                )
            ).scalars().all()
        return sorted(rows)

    path = Path(spec)
    if path.exists():
        # Case is normalised the same way as for the comma list:
        # otherwise the same symbol would reach the exchange in two
        # spellings depending on how it was passed.
        return [
            line.strip().upper()
            for line in path.read_text().splitlines()
            if line.strip()
        ]
    return [s.strip().upper() for s in spec.split(",") if s.strip()]



@app.command("repair-gaps")
def repair_gaps_cmd(
    symbols: str = typer.Option("universe", help="Symbols, file or 'universe'"),
    timeframes: str = typer.Option("15m,1h,4h", help="Comma-separated timeframes"),
    dry_run: bool = typer.Option(False, help="Only show gaps, do not repair"),
) -> None:
    """Targeted download of missing bars.

    The checkpoint stores the bounds of the covered range, while a gap lies inside
    it — the window planner will not see it. Re-downloading the whole series for
    fractions of a percent of data costs more than a targeted repair.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    tfs = [t.strip() for t in timeframes.split(",") if t.strip()]

    async def run() -> None:
        names = await _resolve_symbols(symbols)
        console.print(f"Checking [bold]{len(names)}[/bold] symbols, timeframes {tfs}\n")

        async with BybitClient(settings.bybit, testnet=env.bybit_testnet) as client:
            total_gaps = total_written = 0
            failed: list[str] = []

            for tf in tfs:
                tf_gaps = tf_written = 0
                for name in names:
                    before, missing = await verify_repair(name, tf)
                    if before == 0:
                        continue
                    tf_gaps += before
                    if dry_run:
                        continue
                    res = await repair_symbol(client, name, tf)
                    if not res.ok:
                        failed.append(f"{name}/{tf}")
                        continue
                    after, _ = await verify_repair(name, tf)
                    tf_written += before - after

                mark = "found" if dry_run else "closed"
                console.print(
                    f"  {tf:<5} gaps {tf_gaps:>6}  ·  {mark} {tf_written:>6}"
                )
                total_gaps += tf_gaps
                total_written += tf_written

        await dispose_engine()

        console.print(
            f"\nTotal gaps: [bold]{total_gaps}[/bold]  ·  "
            f"closed: [bold green]{total_written}[/bold green]"
        )
        if failed:
            console.print(f"[red]Failed: {len(failed)}[/red] {failed[:5]}")

        # P-002: the outcome is the exit code. Previously this was always zero,
        # and "Failed" could only be read by eye (DEC-036).
        # A partial failure is expected: some gaps are real exchange outages,
        # there is nothing to download there.
        if dry_run:
            return
        if failed and total_written == 0:
            raise typer.Exit(code=EXIT_FAILED)
        if failed:
            raise typer.Exit(code=EXIT_PARTIAL)

    asyncio.run(run())



@app.command("stream-starts")
def stream_starts_cmd(
    symbols: str = typer.Option("universe", help="Symbols, file or 'universe'"),
    show: int = typer.Option(15, help="How many symbols to show in each group"),
) -> None:
    """Stream start dates and effective_start per Setup.

    Stored for each stream separately: for some pairs OI starts
    earlier than candles, and a single listing date would be wrong for at least
    one of them.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        names = await _resolve_symbols(symbols)
        data = await collect_starts(names)
        await persist_starts(data)

        end = datetime.now(UTC)
        period_start = end - timedelta(days=int(settings.data.backfill_depth_years * 365.25))
        all_streams = frozenset(DataStream(x) for x in settings.data.streams)
        coverage = await compute_coverage(names, all_streams, period_start, end)

        console.print(f"\nSymbols: [bold]{len(names)}[/bold]\n")

        # The divergence of stream starts within a symbol — the very reason
        # they are stored separately.
        spreads = []
        for item in data.values():
            present = [v for v in item.starts.values() if v is not None]
            if len(present) >= 2:
                spreads.append((item.symbol, (max(present) - min(present)).days))
        spreads.sort(key=lambda x: -x[1])

        if spreads:
            table = Table(header_style="bold", title="Largest spread of stream starts")
            table.add_column("symbol")
            table.add_column("spread, days", justify="right")
            for name, days in spreads[:show]:
                table.add_row(name, str(days))
            console.print(table)

        for setup, streams in settings.data.stream_requirements.model_dump().items():
            if setup == "universe_ranking":
                continue
            required = frozenset(DataStream(x) for x in streams)
            readiness = assess_readiness(data, setup, required, coverage)

            console.print(
                f"\n[bold]{setup}[/bold] requires {sorted(s.value for s in required)}"
            )
            for label, count in readiness.summary().items():
                console.print(f"  {label}: [bold]{count}[/bold]")

            single = readiness.single_stream_breakdown()
            if single:
                parts = ", ".join(f"{s.value} ×{n}" for s, n in single.items())
                console.print(
                    f"  [yellow]lost due to ONE stream:[/yellow] {parts}"
                )
            if readiness.missing_streams:
                for name, miss in list(readiness.missing_streams.items())[:show]:
                    console.print(
                        f"    {name}: missing {[s.value for s in miss]}"
                    )
            if readiness.too_sparse:
                console.print(
                    f"  [yellow]coverage below "
                    f"{settings.data.min_stream_coverage:.0%}:[/yellow]"
                )
                for name, thin in list(readiness.too_sparse.items())[:show]:
                    detail = ", ".join(f"{s.value} {c:.0%}" for s, c in thin)
                    console.print(f"    {name}: {detail}")
            if readiness.ready:
                latest = sorted(readiness.ready, key=lambda x: x[1], reverse=True)[:5]
                console.print("  [dim]latest starts:[/dim]")
                for name, when in latest:
                    console.print(f"    {name}: {when.date()}")

        await dispose_engine()

    asyncio.run(run())



@app.command("status-report")
def status_report_cmd(
    output: Path = typer.Option(
        Path("var/reports/status.html"), help="Where to put the HTML"
    ),
    open_browser: bool = typer.Option(False, "--open", help="Open in the browser"),
) -> None:
    """Snapshot of the project state as one static HTML page.

    Data is read from the DB at launch time. No placeholders:
    an uncomputed value is shown as "not computed", not as zero.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        from src.report.content import (
            decisions_block,
            guards_block,
            limitations_block,
            phase_block,
            quality_block,
            read_test_marker,
            readiness_block,
        )
        from src.report.status import (
            gather_data_block,
            gather_universe_block,
            git_info,
            render,
        )

        names = await _resolve_symbols("universe")
        data = await collect_starts(names)
        end = datetime.now(UTC)
        period_start = end - timedelta(
            days=int(settings.data.backfill_depth_years * 365.25)
        )
        all_streams = frozenset(DataStream(x) for x in settings.data.streams)
        coverage = await compute_coverage(names, all_streams, period_start, end)

        required = frozenset(
            DataStream(x) for x in settings.data.stream_requirements.setup_b
        )
        readiness = assess_readiness(data, "setup_b", required, coverage)
        summary = dict(readiness.summary())
        summary["Total in universe"] = readiness.total

        tests, tests_at = read_test_marker()
        blocks = [
            phase_block(tests, tests_at),
            await gather_data_block(),
            await gather_universe_block(),
            readiness_block(summary),
            await quality_block(),
            guards_block(),
            decisions_block(),
            limitations_block(),
            Block(title="Repository", rows=list(git_info().items())),
        ]

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render(blocks, generated=end), encoding="utf-8")
        console.print(f"Report: [bold]{output}[/bold]")

        if open_browser:
            import webbrowser

            webbrowser.open(output.resolve().as_uri())

        await dispose_engine()

    asyncio.run(run())



@app.command("backtest")
def backtest_cmd(
    setup: str = typer.Option("b", help="Which Setup to run: b or a"),
    export: Path | None = typer.Option(
        Path("var/reports/backtest_setup_b.json"), help="Export raw numbers"
    ),
) -> None:
    """Run a Setup: five acceptance thresholds and five mandatory blocks.

    The blocks are always computed, regardless of whether the thresholds passed.
    If they did not — all the more: they explain what exactly broke.
    """
    if setup.lower() != "b":
        raise typer.BadParameter("Setup A comes after B, as fixed in the plan")

    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        from src.backtest.report import build_full_report

        report = await build_full_report(settings)
        _print_backtest(report, settings)

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(_serialise(report), ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
            console.print(f"\nRaw numbers: [bold]{export}[/bold]")

        await dispose_engine()

    asyncio.run(run())


def _serialise(report: dict) -> dict:
    """Numbers only, no objects: the file is read by a human and external
    tools, not loaded back."""
    stats = {}
    for key in ("in_sample", "out_of_sample", "full period"):
        s = report[key]
        stats[key] = {
            "trades": s.trades,
            "wins": s.wins,
            "losses": s.losses,
            "E_R": float(s.expectancy_r) if s.expectancy_r is not None else None,
            "win_rate": float(s.win_rate) if s.win_rate is not None else None,
            "profit_factor": float(s.profit_factor) if s.profit_factor is not None else None,
            "skipped": s.skipped,
        }
    return {
        "stats": stats,
        "thresholds": [
            {"threshold": t.name, "required": t.required, "actual": t.actual, "verdict": t.verdict}
            for t in report["thresholds"].thresholds
        ],
        "drawdown_p95": report["drawdown_p95"],
        "slippage_sweep": [
            {"slippage_pct": s, "trades": n, "E_R": e}
            for s, n, e in report["slippage_sweep"].rows
        ],
        "E_monthly": {m: {"trades": n, "E_R": e} for m, (n, e) in report["E_monthly"].items()},
        "liquidity_monthly": report["liquidity_monthly"],
        "corr_E_liquidity": report["corr_E_liquidity"],
        "marginal": report["marginal"],
        "walk_forward": [
            {
                "test_from": w.test_start.date().isoformat(),
                "to": w.test_end.date().isoformat(),
                "trades": w.trades,
                "E_R": w.expectancy,
            }
            for w in report["walk_forward"]
        ],
    }


def _print_backtest(report: dict, settings) -> None:  # noqa: ANN001
    start, oos_start, end = report["period"]
    console.print(
        f"\n[bold]SETUP B · Trend Continuation[/bold]\n"
        f"Period {start.date()} — {end.date()}  ·  "
        f"in-sample until {oos_start.date()}, out-of-sample after\n"
    )

    t = Table(header_style="bold", title="Statistics")
    for col in ("slice", "trades", "win rate", "E, R", "profit factor", "skipped"):
        t.add_column(col, justify="right" if col != "slice" else "left")
    for label, key in [
        ("in-sample", "in_sample"),
        ("out-of-sample", "out_of_sample"),
        ("full period", "full period"),
    ]:
        s = report[key]
        t.add_row(
            label,
            str(s.trades),
            f"{float(s.win_rate):.1f}%" if s.win_rate is not None else "—",
            f"{float(s.expectancy_r):+.3f}" if s.expectancy_r is not None else "—",
            f"{float(s.profit_factor):.2f}" if s.profit_factor is not None else "—",
            str(s.skipped),
        )
    console.print(t)

    t = Table(header_style="bold", title="Acceptance thresholds")
    for col in ("threshold", "required", "actual", "verdict"):
        t.add_column(col)
    for th in report["thresholds"].thresholds:
        colour = {"PASS": "green", "FAIL": "red"}.get(th.verdict, "yellow")
        t.add_row(th.name, th.required, th.actual, f"[{colour}]{th.verdict}[/{colour}]")
    console.print(t)

    t = Table(header_style="bold", title="Block 1 · slippage sweep")
    for col in ("slippage", "trades", "E, R"):
        t.add_column(col, justify="right")
    for slip, n, e in report["slippage_sweep"].rows:
        t.add_row(f"{slip:.2f}%", str(n), f"{e:+.3f}" if e is not None else "—")
    console.print(t)

    liq = report["liquidity_monthly"]
    me = report["E_monthly"]
    t = Table(header_style="bold", title="Blocks 2 and 5 · E and median turnover by month")
    for col in ("month", "trades", "E, R", "median turnover"):
        t.add_column(col, justify="right" if col != "month" else "left")
    for month in sorted(set(me) | set(liq)):
        n, e = me.get(month, (0, None))
        t.add_row(
            month,
            str(n),
            f"{e:+.3f}" if e is not None else "—",
            f"${liq[month] / 1e6:,.0f}M" if month in liq else "—",
        )
    console.print(t)
    corr = report["corr_E_liquidity"]
    console.print(
        f"Correlation of monthly E with median turnover: "
        f"[bold]{corr:+.3f}[/bold]" if corr is not None
        else "Correlation of E with turnover: [yellow]not computed[/yellow]"
    )

    console.print("\n[bold]Block 3 · contribution of marginal members[/bold]")
    for k, v in report["marginal"].items():
        console.print(f"  {k}: [bold]{v}[/bold]")

    wf = report["walk_forward"]
    if wf:
        t = Table(header_style="bold", title="Block 4 · walk-forward")
        for col in ("test window", "trades", "E, R"):
            t.add_column(col, justify="right" if col != "test window" else "left")
        for w in wf:
            t.add_row(
                f"{w.test_start.date()} — {w.test_end.date()}",
                str(w.trades),
                f"{w.expectancy:+.3f}" if w.expectancy is not None else "—",
            )
        console.print(t)
        positive = sum(1 for w in wf if w.expectancy is not None and w.expectancy > 0)
        counted = sum(1 for w in wf if w.expectancy is not None)
        console.print(
            f"Windows with positive E: [bold]{positive}[/bold] of {counted}"
        )
        console.print(
            "[yellow]Windows OVERLAP: neighbours share "
            f"{settings.backtest.walk_forward.shared_months} training months. "
            "The estimates are not independent — confidence intervals, t-statistics "
            "and p-values must not be computed from them.[/yellow]"
        )



@app.command("excursion-report")
def excursion_report_cmd(
    setup: str = typer.Option("a", help="Which Setup to measure: a, b, c or all"),
    end: str | None = typer.Option(
        None, help="Period end YYYY-MM-DD. Default — the last day with data"
    ),
    export: Path | None = typer.Option(
        Path("var/reports/excursion.json"), help="Export raw numbers"
    ),
) -> None:
    """Distribution of price movement after entry, in units of R. In-sample ONLY.

    Answers the question that must be asked before choosing a target: what
    movement the market gives at all over the holding horizon. The measurement
    is diagnostic — it looks forward from the entry and is therefore computed
    as a separate pass over the finished trade list, returning nothing
    to the signal function.

    Out-of-sample is not touched here for any Setup, including
    the rejected B: diagnosing a design is no reason to spend
    a sample that can be spent only once.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    wanted = {
        "a": ["a"], "b": ["b"], "c": ["c"],
        "both": ["a", "b"], "all": ["a", "b", "c"],
    }.get(setup.lower())
    if wanted is None:
        raise typer.BadParameter("setup must be a, b, c, both or all")

    async def run() -> None:
        from src.backtest.excursion import (
            attainability,
            distribution,
            measure,
            target_at_quantile,
        )
        from src.backtest.runner import (
            load_bars,
            run_setup_a,
            run_setup_b,
            run_setup_c,
        )
        from src.strategy.setup_a import SetupAParams
        from src.strategy.setup_b import SetupBParams
        from src.strategy.setup_c import SetupCParams

        stop_at = await _data_end(end)
        months = settings.backtest.in_sample_months + settings.backtest.out_of_sample_months
        start = stop_at - timedelta(days=30.44 * months)
        oos_start = stop_at - timedelta(days=30.44 * settings.backtest.out_of_sample_months)

        console.print(
            f"\n[bold]PRICE MOVEMENT DISTRIBUTION AFTER ENTRY[/bold]\n"
            f"in-sample {start.date()} — {oos_start.date()}  ·  "
            f"out-of-sample untouched"
        )

        payload: dict[str, object] = {
            "period": {
                "in_sample_from": start.date().isoformat(),
                "in_sample_to": oos_start.date().isoformat(),
            }
        }

        for name in wanted:
            if name == "a":
                params = SetupAParams.from_config(settings.strategy["setup_a"])
                timeframe, hours = params.entry_timeframe, params.time_stop_hours
                title = f"SETUP A · {settings.strategy['setup_a']['name']}"
                extra = f"exit: {params.exit_mode.value}"
                result = await run_setup_a(settings, start, oos_start)
            elif name == "c":
                cparams = SetupCParams.from_config(settings.strategy["setup_c"])
                timeframe, hours = cparams.entry_timeframe, cparams.time_stop_hours
                title = f"SETUP C · {settings.strategy['setup_c']['name']}"
                extra = (
                    f"target: {cparams.target_r}R, stop {cparams.stop_atr_mult}xATR "
                    f"capped at {cparams.max_stop_pct}%"
                )
                result = await run_setup_c(settings, start, oos_start)
            else:
                bparams = SetupBParams.from_config(settings.strategy["setup_b"])
                timeframe, hours = "4h", bparams.time_stop_hours
                title = f"SETUP B · {settings.strategy['setup_b']['name']}"
                extra = f"target: {bparams.target_r}R"
                result = await run_setup_b(settings, start, oos_start)

            time_stop = timedelta(hours=hours)
            excursions = []
            for symbol, outcomes in result.per_symbol.items():
                bars = await load_bars(symbol, timeframe, start, oos_start)
                excursions.extend(measure(bars, outcomes, time_stop, symbol=symbol))

            payload[name] = _print_excursions(
                title, extra, timeframe, hours, result, excursions,
                distribution, attainability, target_at_quantile,
            )

        if export is not False:
            path = export or Path(f"var/reports/filter_sweep_{setup.lower()}.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
            console.print(f"\nRaw numbers: [bold]{path}[/bold]")

        await dispose_engine()

    asyncio.run(run())


async def _data_end(explicit: str | None, timeframe: str = "15m") -> datetime:
    """Period end. Default — the last day for which candles exist.

    Anchoring to `now()` is wrong here: collection lags the calendar, and a window
    counted from today drifts into a stretch without data.
    Then two measurements taken on different days are computed over different
    periods and silently become incomparable.
    """
    if explicit is not None:
        return datetime.fromisoformat(explicit).replace(tzinfo=UTC)

    from sqlalchemy import func, select

    from src.core.db import Ohlcv, session_scope

    async with session_scope() as s:
        latest = (
            await s.execute(
                select(func.max(Ohlcv.ts)).where(Ohlcv.timeframe == timeframe)
            )
        ).scalar()
    if latest is None:
        raise typer.BadParameter("no candles in the DB — nothing to count the period from")
    return latest.replace(hour=0, minute=0, second=0, microsecond=0)


def _print_excursions(  # noqa: ANN001, PLR0913
    title, extra, timeframe, hours, result, excursions,
    distribution, attainability, target_at_quantile,
) -> dict:
    """Prints the distribution and returns the same numbers for export."""
    valid = [e for e in excursions if e.valid]
    dropped = len(excursions) - len(valid)

    console.print(f"\n[bold]{title}[/bold]")
    console.print(
        f"entry on {timeframe}, time stop {hours}h, {extra}\n"
        f"trades: {len(excursions)}  ·  measured: {len(valid)}  ·  "
        f"skipped (horizon beyond data): {dropped}"
    )

    if not valid:
        console.print("[bold red]nothing to measure[/bold red]")
        return {"trades": len(excursions), "measured": 0}

    attainable = [float(e.attainable_r) for e in valid]
    horizon = [float(e.horizon_r) for e in valid]
    adverse = [float(e.adverse_r) for e in valid]

    d_att, d_hor, d_adv = (distribution(v) for v in (attainable, horizon, adverse))

    table = Table(show_header=True, header_style="bold")
    table.add_column("quantity")
    for column in ("p10", "p25", "median", "p75", "p90", "mean", "max"):
        table.add_column(column, justify="right")

    def row(label: str, d) -> None:  # noqa: ANN001
        table.add_row(
            label,
            *(
                f"{v:.2f}" if v is not None else "—"
                for v in (d.p10, d.p25, d.median, d.p75, d.p90, d.mean, d.maximum)
            ),
        )

    row("move in our favour, R", d_att)
    row("same, without stop", d_hor)
    row("move against us, R", d_adv)
    console.print(table)

    hits = Table(show_header=True, header_style="bold")
    hits.add_column("target, R", justify="right")
    hits.add_column("hit", justify="right")
    hits.add_column("share", justify="right")
    hits.add_column("P-005", justify="left")
    rows = attainability(attainable)
    for target, n, share in rows:
        hits.add_row(
            f"{target:.2f}", str(n), f"{share:.1%}",
            "passes" if share >= 0.10 else "below threshold",
        )
    console.print(hits)

    chosen = target_at_quantile(attainable, 0.75)
    taken = sum(1 for v in attainable if v >= chosen) / len(attainable)
    console.print(
        f"target at the 75th percentile of movement: [bold]{chosen:.2f}R[/bold] "
        f"(hit in {taken:.1%} of trades)"
    )

    bars_to_peak = [e.bars_to_peak for e in valid if e.bars_to_peak is not None]
    if bars_to_peak:
        d_bars = distribution([float(b) for b in bars_to_peak])
        console.print(
            f"bars to the maximum: median {d_bars.median:.0f}, "
            f"p90 {d_bars.p90:.0f} of {int(hours * 60 / _minutes(timeframe))}"
        )

    return {
        "trades": len(excursions),
        "measured": len(valid),
        "skipped": dropped,
        "move_R": _dist_dict(d_att),
        "move_no_stop_R": _dist_dict(d_hor),
        "adverse_R": _dist_dict(d_adv),
        "attainability": [
            {"target_R": t, "hit": n, "share": share} for t, n, share in rows
        ],
        "target_p75": chosen,
        "rejections": result.rejections,
    }


def _minutes(timeframe: str) -> int:
    from src.strategy.setup_a import parse_timeframe

    return int(parse_timeframe(timeframe).total_seconds() // 60)


def _dist_dict(d) -> dict:  # noqa: ANN001
    return {
        "n": d.n, "p10": d.p10, "p25": d.p25, "median": d.median,
        "p75": d.p75, "p90": d.p90, "mean": d.mean, "max": d.maximum,
    }



@app.command("p005")
def p005_cmd(
    setup: str = typer.Option("a", help="Which Setup to check: a, b or c"),
    exit_mode: str | None = typer.Option(
        None, help="Override the exit of Setup A: vwap or fixed_r"
    ),
    target_r: float | None = typer.Option(
        None, help="Target in R with exit_mode=fixed_r. Only for comparing measurements"
    ),
    end: str | None = typer.Option(None, help="Period end YYYY-MM-DD"),
) -> None:
    """P-005: are the target and the time stop compatible. In-sample ONLY.

    Prints the distribution of exit reasons and a verdict. Below the target-hit
    threshold the run is invalid — it measures the holding window,
    not the Setup's hypothesis.

    Out-of-sample is not touched: the design is checked on the
    data where a mistake can still be fixed.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    if setup.lower() not in {"a", "b", "c"}:
        raise typer.BadParameter("setup must be a, b or c")

    async def run() -> None:
        from dataclasses import replace

        from src.backtest.accounting import summarize
        from src.backtest.report import (
            MIN_TARGET_HIT_RATE,
            UNKNOWN,
            RunInvalid,
            assert_run_is_valid,
            assert_target_is_payable,
            exit_breakdown,
            target_economics,
        )
        from src.backtest.runner import run_setup_a, run_setup_b, run_setup_c
        from src.strategy.setup_a import ExitMode, SetupAParams

        stop_at = await _data_end(end)
        months = settings.backtest.in_sample_months + settings.backtest.out_of_sample_months
        start = stop_at - timedelta(days=30.44 * months)
        oos_start = stop_at - timedelta(days=30.44 * settings.backtest.out_of_sample_months)

        if setup.lower() == "a":
            params = SetupAParams.from_config(settings.strategy["setup_a"])
            if exit_mode is not None:
                params = replace(params, exit_mode=ExitMode(exit_mode))
            if target_r is not None:
                params = replace(params, target_r=Decimal(str(target_r)))
            label = f"SETUP A · exit {params.exit_mode.value}"
            if params.exit_mode is ExitMode.FIXED_R:
                label += f" {params.target_r}R"
            result = await run_setup_a(settings, start, oos_start, params=params)
            timeframe, hours = params.entry_timeframe, params.time_stop_hours
        elif setup.lower() == "c":
            from src.strategy.setup_c import SetupCParams

            cparams = SetupCParams.from_config(settings.strategy["setup_c"])
            label = (
                f"SETUP C · target {cparams.target_r}R, "
                f"stop {cparams.stop_atr_mult}xATR"
            )
            result = await run_setup_c(settings, start, oos_start)
            timeframe, hours = cparams.entry_timeframe, cparams.time_stop_hours
        else:
            from src.strategy.setup_b import SetupBParams

            bparams = SetupBParams.from_config(settings.strategy["setup_b"])
            label = f"SETUP B · exit fixed_r {bparams.target_r}R"
            result = await run_setup_b(settings, start, oos_start)
            timeframe, hours = "4h", bparams.time_stop_hours

        console.print(
            f"\n[bold]=== P-005 · {label} · in-sample ONLY ===[/bold]\n"
            f"period: {start.date()} — {oos_start.date()} "
            f"({settings.backtest.in_sample_months} months)\n"
            f"entry on {timeframe}, time stop {hours}h\n"
        )
        console.print(
            f"symbols considered   : {result.symbols_considered}\n"
            f"symbols with trades  : {result.symbols_traded}\n"
            f"trades opened        : {len(result.outcomes)}\n"
        )

        breakdown = exit_breakdown(result.outcomes)
        for reason, n in sorted(breakdown.counts.items(), key=lambda kv: -kv[1]):
            console.print(f"  {reason:<15}{n:>4}{n / breakdown.total:>8.1%}")

        console.print(
            f"\ntarget hit rate: {breakdown.target_rate:.1%}  "
            f"(P-005 threshold: {MIN_TARGET_HIT_RATE:.0%})"
        )
        economics = target_economics(result.outcomes, settings)
        checks = (
            ("P-005", lambda: assert_run_is_valid(breakdown, label)),
            ("P-006", lambda: assert_target_is_payable(economics, label)),
        )
        failed: list[str] = []
        for name, check in checks:
            try:
                check()
            except RunInvalid as exc:
                failed.append(f"[bold red]{name}: FAIL[/bold red]\n{exc}")
            else:
                console.print(f"[bold green]{name}: PASS[/bold green]")
        for message in failed:
            console.print(message)
        console.print(
            "[bold red]VERDICT: RUN INVALID[/bold red]"
            if failed
            else "[bold green]VERDICT: run valid[/bold green]"
        )

        # How far away the target was. With a VWAP exit this is
        # not a parameter but a quantity the market computed — and knowing
        # its distribution matters more than any assigned number.
        distances = [
            float(abs(o.trade.target - o.trade.entry) / o.trade.risk_per_unit)
            for o in result.outcomes
            if o.trade.target is not None and o.trade.risk_per_unit > 0
        ]
        if distances:
            from src.backtest.excursion import distribution

            d = distribution(distances)
            console.print(f"\ntarget distance, R: {d.describe()}")

        # The same distance as a percentage move. The P-005 threshold checks
        # compatibility of the target with the WINDOW, and this one with COSTS: a target
        # that fits within the round-trip fee is attainable and pointless.
        moves = [
            float(abs(o.trade.target - o.trade.entry) / o.trade.entry * 100)
            for o in result.outcomes
            if o.trade.target is not None and o.trade.entry > 0
        ]
        if moves:
            from src.backtest.excursion import distribution

            d = distribution(moves)
            floor = float(settings.strategy["min_move_pct"])
            below = sum(1 for m in moves if m < floor)
            console.print(
                f"target distance, %: {d.describe()}\n"
                f"below threshold min_move_pct={floor}%: {below} of {len(moves)} "
                f"({below / len(moves):.1%})"
            )

        stats = summarize(result.outcomes)
        console.print(
            f"\nE = {_fmt(stats.expectancy_r, 3) if stats.expectancy_r is not None else UNKNOWN}R"
            f"  ·  win rate "
            f"{_fmt(stats.win_rate, 1) + '%' if stats.win_rate is not None else UNKNOWN}"
            f"  ·  trades in statistics {stats.trades}"
            f"  ·  skipped {stats.skipped}"
        )
        console.print(
            "[dim]small sample: with this many trades E is not statistically "
            "different from zero — this is a design diagnostic, "
            "not an estimate of edge[/dim]"
            if stats.trades < 60
            else ""
        )

        if result.rejections:
            console.print("\nRisk Manager rejections:")
            for code, n in sorted(result.rejections.items(), key=lambda kv: -kv[1]):
                console.print(f"  {code:<20}{n:>5}")

        await dispose_engine()

    asyncio.run(run())



@app.command("funnel-report")
def funnel_report_cmd(
    end: str | None = typer.Option(None, help="Period end YYYY-MM-DD"),
    export: Path | None = typer.Option(
        Path("var/reports/funnel_a.json"), help="Export raw numbers"
    ),
) -> None:
    """Where Setup A signals die. In-sample ONLY.

    A Setup report says how many trades came out, and that is useless
    when there are twenty-four of them. The funnel says from how many bars they
    came and at which check the rest were filtered out — without this,
    frequency calibration boils down to blindly trying thresholds.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        from src.backtest.excursion import distribution
        from src.backtest.funnel import make_funnel
        from src.backtest.runner import run_setup_a

        stop_at = await _data_end(end)
        months = settings.backtest.in_sample_months + settings.backtest.out_of_sample_months
        start = stop_at - timedelta(days=30.44 * months)
        oos_start = stop_at - timedelta(days=30.44 * settings.backtest.out_of_sample_months)

        funnel = make_funnel()
        result = await run_setup_a(settings, start, oos_start, funnel=funnel)

        if not funnel.is_monotone():
            raise RuntimeError(
                "funnel counters do not decrease — stage marking diverged "
                "from the order of checks"
            )

        console.print(
            f"\n[bold]SIGNAL FUNNEL · SETUP A[/bold]\n"
            f"in-sample {start.date()} — {oos_start.date()}  ·  "
            f"symbols: {result.symbols_considered}"
        )

        table = Table(show_header=True, header_style="bold")
        table.add_column("stage")
        table.add_column("remaining", justify="right")
        table.add_column("from start", justify="right")
        table.add_column("passed stage", justify="right")
        rows = funnel.rows()
        for title, n, share, step in rows:
            table.add_row(
                title,
                f"{n:,}".replace(",", " "),
                f"{share:.4%}" if share is not None else "—",
                f"{step:.1%}" if step is not None else "—",
            )
        console.print(table)

        console.print(
            f"\nRisk Manager accepted: {len(result.outcomes)} of "
            f"{funnel.counts['signal']}"
        )
        for code, n in sorted(result.rejections.items(), key=lambda kv: -kv[1]):
            console.print(f"  {code:<20}{n:>5}")

        # What was lost is not random: comparing the distributions shows
        # whether the remainder is skewed towards tight stops.
        all_stops = distribution(funnel.stop_pct)
        funded = distribution(funnel.stop_pct_funded)
        console.print(
            f"\nstop distance, % of entry\n"
            f"  all signals : {all_stops.describe()}\n"
            f"  funded      : {funded.describe()}"
        )
        if all_stops.median is not None and funded.median is not None:
            console.print(
                f"  median shifted from {all_stops.median:.2f}% "
                f"to {funded.median:.2f}%"
            )

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(
                    {
                        "period": {
                            "from": start.date().isoformat(),
                            "to": oos_start.date().isoformat(),
                        },
                        "stages": [
                            {"stage": t_, "remaining": n, "from_start": s, "passed": st}
                            for t_, n, s, st in rows
                        ],
                        "trades": len(result.outcomes),
                        "rejections": result.rejections,
                        "stop_all_pct": _dist_dict(all_stops),
                        "stop_funded_pct": _dist_dict(funded),
                    },
                    ensure_ascii=False,
                    indent=1,
                    default=str,
                ),
                encoding="utf-8",
            )
            console.print(f"\nRaw numbers: [bold]{export}[/bold]")

        await dispose_engine()

    asyncio.run(run())



@app.command("filter-sweep")
def filter_sweep_cmd(
    setup: str = typer.Option("a", help="Which Setup to sweep: a or c"),
    end: str | None = typer.Option(None, help="Period end YYYY-MM-DD"),
    export: Path | None = typer.Option(
        None, help="Export raw numbers"
    ),
) -> None:
    """Sweep over the Setup's knobs. In-sample ONLY.

    Read by the rule written in config/settings.yaml BEFORE the run:
    the sweep is a result, not a way to pick a value. A flat E
    means the filter cuts frequency and carries no edge;
    a monotonically falling one — the filter is load-bearing, frequency cannot come from it.
    Choosing by maximum E is forbidden.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        import statistics
        from dataclasses import replace

        from src.backtest.accounting import summarize
        from src.backtest.report import (
            UNKNOWN,
            RunInvalid,
            assert_run_is_valid,
            exit_breakdown,
            target_economics,
        )
        from src.backtest.runner import sweep_setup_a, sweep_setup_c
        from src.strategy.setup_a import SetupAParams

        stop_at = await _data_end(end)
        months = settings.backtest.in_sample_months + settings.backtest.out_of_sample_months
        start = stop_at - timedelta(days=30.44 * months)
        oos_start = stop_at - timedelta(days=30.44 * settings.backtest.out_of_sample_months)

        if setup.lower() not in {"a", "c"}:
            raise typer.BadParameter("setup must be a or c")

        # The frequency requirement comes from the acceptance thresholds, not from wishes.
        need_oos = 60
        need_is = int(
            need_oos
            * settings.backtest.in_sample_months
            / settings.backtest.out_of_sample_months
        )

        if setup.lower() == "c":
            from src.strategy.setup_c import SetupCParams

            base = SetupCParams.from_config(settings.strategy["setup_c"])
            sweeper = sweep_setup_c
            title = "SETUP C"
            knobs = [
                (
                    "breakout window, bars",
                    "breakout_lookback",
                    base.breakout_lookback_sensitivity,
                    lambda v: {"breakout_lookback": int(v)},
                    base.breakout_lookback,
                ),
                (
                    "stop, xATR",
                    "stop_atr_mult",
                    base.stop_atr_mult_sensitivity,
                    lambda v: {"stop_atr_mult": v},
                    base.stop_atr_mult,
                ),
                (
                    "funding, lower percentile",
                    "funding_band",
                    base.funding_band_sensitivity,
                    lambda v: {
                        "funding_min_percentile": float(v),
                        "funding_max_percentile": 100 - float(v),
                    },
                    base.funding_min_percentile,
                ),
                (
                    "holding window, h",
                    "time_stop_hours",
                    base.time_stop_hours_sensitivity,
                    lambda v: {"time_stop_hours": int(v)},
                    base.time_stop_hours,
                ),
                (
                    "target, R",
                    "target_r",
                    base.target_r_sensitivity,
                    lambda v: {"target_r": v},
                    base.target_r,
                ),
            ]
        else:
            base = SetupAParams.from_config(settings.strategy["setup_a"])
            sweeper = sweep_setup_a
            title = "SETUP A"
            knobs = [
                (
                    "OI drop per hour, %",
                    "oi_drop_pct_1h",
                    base.oi_drop_pct_1h_sensitivity,
                    lambda v: {"oi_drop_pct_1h": v},
                    base.oi_drop_pct_1h,
                ),
                (
                    "candle size in ATR",
                    "candle_range_atr_mult",
                    base.candle_range_atr_mult_sensitivity,
                    lambda v: {"candle_range_atr_mult": v},
                    base.candle_range_atr_mult,
                ),
                (
                    "volume spike, sigmas",
                    "volume_zscore_min",
                    base.volume_zscore_min_sensitivity,
                    lambda v: {"volume_zscore_min": float(v)},
                    base.volume_zscore_min,
                ),
                (
                    "funding, percentile",
                    "funding_percentile",
                    base.funding_percentile_sensitivity,
                    lambda v: {
                        "funding_lower_percentile": float(v),
                        "funding_upper_percentile": 100 - float(v),
                    },
                    base.funding_lower_percentile,
                ),
            ]

        console.print(
            f"\n[bold]KNOB SWEEP · {title}[/bold]\n"
            f"in-sample {start.date()} — {oos_start.date()}\n"
            f"in-sample trades needed: [bold]{need_is}[/bold] "
            f"(from the threshold \">= {need_oos} on OOS\", rescaled by window length)"
        )

        payload: dict[str, object] = {
            "period": {
                "from": start.date().isoformat(),
                "to": oos_start.date().isoformat(),
            },
            "trades_needed_is": need_is,
            "knobs": {},
        }

        for title, key, grid, build, base_value in knobs:
            variants = [(str(v), replace(base, **build(v))) for v in grid]
            results = await sweeper(settings, start, oos_start, variants)

            table = Table(show_header=True, header_style="bold")
            table.add_column(title, justify="right")
            table.add_column("trades", justify="right")
            table.add_column("enough?", justify="left")
            table.add_column("E, R", justify="right")
            table.add_column("win rate", justify="right")
            table.add_column("target hit", justify="right")
            table.add_column("P-005", justify="left")

            rows = []
            for label, _ in variants:
                result = results[label]
                stats = summarize(result.outcomes)
                breakdown = exit_breakdown(result.outcomes)
                try:
                    assert_run_is_valid(breakdown, label)
                except RunInvalid as exc:
                    # "Not computed" and "failed" are different things, and
                    # lumping them into one cell would bring back the very
                    # misreading the guard was introduced for.
                    p005 = (
                        "not computed" if "NOT DECIDED" in str(exc) else "FAIL"
                    )
                else:
                    p005 = "PASS"
                marker = " ←" if label == str(base_value) else ""
                table.add_row(
                    label + marker,
                    str(stats.trades),
                    "yes" if stats.trades >= need_is else "no",
                    _fmt(stats.expectancy_r, 3)
                    if stats.expectancy_r is not None
                    else UNKNOWN,
                    _fmt(stats.win_rate, 1) + "%"
                    if stats.win_rate is not None
                    else UNKNOWN,
                    f"{breakdown.target_rate:.1%}" if breakdown.total else "—",
                    p005,
                )
                rows.append(
                    {
                        "value": label,
                        "base": label == str(base_value),
                        "trades": stats.trades,
                        "E_R": float(stats.expectancy_r)
                        if stats.expectancy_r is not None
                        else None,
                        "win_rate": float(stats.win_rate)
                        if stats.win_rate is not None
                        else None,
                        "target_hit": breakdown.target_rate if breakdown.total else None,
                        "P-005": p005,
                        "target_economics": target_economics(
                            result.outcomes, settings
                        ).describe(),
                    }
                )
            console.print()
            console.print(table)
            payload["knobs"][key] = rows

            measured = [r["E_R"] for r in rows if r["E_R"] is not None]
            if len(measured) >= 2:
                console.print(
                    f"[dim]E spread across the sweep: "
                    f"{min(measured):+.3f} … {max(measured):+.3f}R[/dim]"
                )

        if setup.lower() == "a":
            # A single-knob sweep runs into power: at base values of the
            # other filters the sample drops to dozens of trades in places,
            # and E on it is noise. So the trigger is swept once more
            # in the WEAKEST corner of the declared grid, where the sample
            # is sufficient.
            #
            # This tests not tuning but the hypothesis: if a cascade reverts,
            # then the sharper the OI drop, the stronger the reversion. The absence
            # of such a dependence refutes the Setup, and no tuning
            # fixes that.
            corner = replace(
                base,
                candle_range_atr_mult=min(base.candle_range_atr_mult_sensitivity),
                volume_zscore_min=min(base.volume_zscore_min_sensitivity),
                funding_lower_percentile=max(base.funding_percentile_sensitivity),
                funding_upper_percentile=100 - max(base.funding_percentile_sensitivity),
            )
            trigger_variants = [
                (str(v), replace(corner, oi_drop_pct_1h=v))
                for v in base.oi_drop_pct_1h_sensitivity
            ]
            trigger_results = await sweep_setup_a(
                settings, start, oos_start, trigger_variants
            )

            console.print(
                "\n[bold]HYPOTHESIS ON A POWERED SAMPLE[/bold]\n"
                "weakest corner of the filter grid, sweep over the trigger.\n"
                "if a cascade reverts — the sharper the drop, the higher E"
            )
            table = Table(show_header=True, header_style="bold")
            table.add_column("OI drop, %", justify="right")
            table.add_column("trades", justify="right")
            table.add_column("enough?", justify="left")
            table.add_column("E, R", justify="right")
            table.add_column("SE", justify="right")
            table.add_column("E/SE", justify="right")
            table.add_column("win rate", justify="right")

            trigger_rows = []
            for label, _ in trigger_variants:
                result = trigger_results[label]
                stats = summarize(result.outcomes)
                rs = [float(o.r_multiple) for o in result.outcomes if o.valid]
                se = statistics.stdev(rs) / len(rs) ** 0.5 if len(rs) > 1 else None
                e = float(stats.expectancy_r) if stats.expectancy_r is not None else None
                table.add_row(
                    label,
                    str(stats.trades),
                    "yes" if stats.trades >= need_is else "no",
                    f"{e:+.3f}" if e is not None else UNKNOWN,
                    f"{se:.3f}" if se is not None else "—",
                    f"{e / se:+.2f}" if e is not None and se else "—",
                    _fmt(stats.win_rate, 1) + "%" if stats.win_rate is not None else UNKNOWN,
                )
                trigger_rows.append(
                    {
                        "OI_drop_pct": label,
                        "trades": stats.trades,
                        "E_R": e,
                        "SE": se,
                        "win_rate": float(stats.win_rate)
                        if stats.win_rate is not None
                        else None,
                    }
                )
            console.print(table)
            payload["hypothesis_on_powered_sample"] = trigger_rows


        console.print(
            "\n[dim]Read by the rule in config/settings.yaml: the sweep "
            "is a result. Choosing a value by maximum E is forbidden.[/dim]"
        )

        if export:
            export.parent.mkdir(parents=True, exist_ok=True)
            export.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                encoding="utf-8",
            )
            console.print(f"\nRaw numbers: [bold]{export}[/bold]")

        await dispose_engine()

    asyncio.run(run())



@app.command("insample-report")
def insample_report_cmd(
    setup: str = typer.Option("c", help="Which Setup: a, b or c"),
    end: str | None = typer.Option(None, help="Period end YYYY-MM-DD"),
    export: Path | None = typer.Option(
        None, help="Export raw numbers (default var/reports/insample_<setup>.json)"
    ),
) -> None:
    """Full battery of in-sample checks. OUT-OF-SAMPLE IS NOT TOUCHED.

    The command physically cannot compute on OOS: the window ends
    at its start, and there is no other argument. A flag that one could forget
    to turn off would be worse here than a missing capability — the
    out-of-sample is spent once and cannot be restored.

    Everything that can be learned without spending it is computed: frequency vs
    the power requirement, both target guards, the movement distribution,
    the sliding window, concentration and the slippage sweep.
    """
    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)
    name = setup.lower()
    if name not in {"a", "b", "c"}:
        raise typer.BadParameter("setup must be a, b or c")

    async def run() -> None:
        import statistics as _stats

        from src.backtest.accounting import summarize
        from src.backtest.excursion import attainability, distribution, measure
        from src.backtest.report import (
            UNKNOWN,
            RunInvalid,
            assert_run_is_valid,
            assert_target_is_payable,
            concentration_check,
            exit_breakdown,
            monthly_btc_range,
            regime_sensitivity,
            target_economics,
            walk_forward,
            window_dispersion,
        )
        from src.backtest.runner import load_bars

        stop_at = await _data_end(end)
        months = settings.backtest.in_sample_months + settings.backtest.out_of_sample_months
        start = stop_at - timedelta(days=30.44 * months)
        oos_start = stop_at - timedelta(days=30.44 * settings.backtest.out_of_sample_months)

        runner, params, title = await _setup_runner(settings, name)
        timeframe = getattr(params, "entry_timeframe", "4h")
        hours = params.time_stop_hours

        console.print(
            f"\n[bold]{title} · IN-SAMPLE ONLY[/bold]\n"
            f"{start.date()} — {oos_start.date()} "
            f"({settings.backtest.in_sample_months} months)\n"
            f"[dim]out-of-sample {oos_start.date()} — {stop_at.date()} "
            f"untouched[/dim]"
        )

        result = await runner(settings, start, oos_start)
        stats = summarize(result.outcomes)
        payload: dict[str, object] = {
            "setup": name,
            "period": {"from": start.date().isoformat(), "to": oos_start.date().isoformat()},
        }

        # 1. Frequency vs the power requirement.
        need_oos = 60
        need_is = int(
            need_oos
            * settings.backtest.in_sample_months
            / settings.backtest.out_of_sample_months
        )
        enough = stats.trades >= need_is
        console.print(
            f"\n[bold]1. FREQUENCY[/bold]\n"
            f"trades {stats.trades} with {need_is} required "
            f"(threshold \">= {need_oos} on OOS\" rescaled by window length): "
            + ("[bold green]enough[/bold green]" if enough else "[bold red]NOT ENOUGH[/bold red]")
            + f"\nsymbols with trades: {result.symbols_traded} "
            f"of {result.symbols_considered}"
        )
        payload["frequency"] = {
            "trades": stats.trades, "needed": need_is, "enough": enough,
            "symbols": result.symbols_traded,
        }

        # 2. Target guards.
        breakdown = exit_breakdown(result.outcomes)
        economics = target_economics(result.outcomes, settings)
        console.print("\n[bold]2. TARGET AND WINDOW[/bold]")
        for reason, n in sorted(breakdown.counts.items(), key=lambda kv: -kv[1]):
            console.print(f"  {reason:<15}{n:>5}{n / breakdown.total:>8.1%}")
        guards: dict[str, str] = {}
        for label, check in (
            ("P-005", lambda: assert_run_is_valid(breakdown, title)),
            ("P-006", lambda: assert_target_is_payable(economics, title)),
        ):
            try:
                check()
            except RunInvalid as exc:
                verdict = "not computed" if "NOT DECIDED" in str(exc) else "FAIL"
                console.print(f"  [bold red]{label}: {verdict}[/bold red] — {exc}")
            else:
                verdict = "PASS"
                console.print(f"  [bold green]{label}: PASS[/bold green]")
            guards[label] = verdict
        console.print(f"  {economics.describe()}")
        payload["guards"] = guards
        payload["exits"] = breakdown.counts

        # 3. What the market gives over this horizon.
        console.print("\n[bold]3. MOVEMENT AFTER ENTRY[/bold]")
        excursions = []
        for symbol, outcomes in result.per_symbol.items():
            bars = await load_bars(symbol, timeframe, start, oos_start)
            excursions.extend(
                measure(bars, outcomes, timedelta(hours=hours), symbol=symbol)
            )
        valid = [float(e.attainable_r) for e in excursions if e.valid]
        if valid:
            d = distribution(valid)
            console.print(f"  {d.describe()}")
            rows = attainability(valid)
            taken = {t_: share for t_, _n, share in rows}
            target = float(getattr(params, "target_r", 0) or 0)
            nearest = min(taken, key=lambda x: abs(x - target)) if taken else None
            if nearest is not None:
                console.print(
                    f"  target {target:.2f}R is hit in {taken[nearest]:.1%} of trades"
                )
            payload["move_R"] = {
                "median": d.median, "p75": d.p75, "p90": d.p90, "n": d.n
            }
        else:
            console.print("  [dim]nothing to measure[/dim]")

        # 4. Sliding window — the main discriminator when the OOS of a related
        # Setup is already spent (DEC-018).
        console.print("\n[bold]4. SLIDING WINDOW[/bold]")
        windows = await walk_forward(settings, start, oos_start, result)
        measured = [w for w in windows if w.expectancy is not None]
        if len(measured) >= settings.backtest.walk_forward.min_windows:
            positive = sum(1 for w in measured if w.expectancy > 0)
            values = [w.expectancy for w in measured]
            console.print(
                f"  windows {len(measured)}, positive {positive} "
                f"({positive / len(measured):.0%})\n"
                f"  E from {min(values):+.3f} to {max(values):+.3f}, "
                f"median {_stats.median(values):+.3f}"
            )
            console.print(
                "  [dim]windows OVERLAP: estimates are not independent, "
                "confidence intervals and p-values must not be computed from them[/dim]"
            )
            # "Seven of twelve" says nothing by itself.
            # The question is whether the spread is WIDER than follows from the window
            # size: wider — regime dependence, the same — the windows
            # are simply small.
            spread = window_dispersion(windows, result.outcomes)
            console.print(f"  {spread.describe()}")
            # If the spread is wider than expected, there must be a
            # measurable cause. The BTC range is a proxy for trendiness:
            # a trend Setup needs the price to go somewhere
            # and not come back.
            corr, months = regime_sensitivity(
                result.outcomes, await monthly_btc_range()
            )
            if corr is not None:
                console.print(
                    f"  correlation of monthly E with BTC range: "
                    f"[bold]{corr:+.3f}[/bold] over {months} months"
                )
                payload["regime"] = {"corr_with_BTC_range": corr,
                                    "months": months}
            payload["window_spread"] = {
                "observed": spread.observed_sd,
                "expected": spread.expected_sd,
                "ratio": spread.ratio,
                "regime_dependent": spread.regime_dependent,
            }
            payload["windows"] = {
                "total": len(measured), "positive": positive,
                "median_E": _stats.median(values),
            }
        else:
            console.print(
                f"  [dim]windows {len(measured)} with a minimum of "
                f"{settings.backtest.walk_forward.min_windows} — no conclusions drawn[/dim]"
            )

        # 5. Concentration.
        base_e, without, top = concentration_check(result.per_symbol)
        console.print(
            f"\n[bold]5. CONCENTRATION[/bold]\n"
            f"  E = {base_e if base_e is None else f'{base_e:+.3f}'}R, "
            f"without the two best ({', '.join(top) or '—'}) = "
            f"{without if without is None else f'{without:+.3f}'}R"
        )
        payload["concentration"] = {"E": base_e, "without_two": without, "best": top}

        e_text = (
            _fmt(stats.expectancy_r, 3) if stats.expectancy_r is not None else UNKNOWN
        )
        wr_text = (
            _fmt(stats.win_rate, 1) + "%" if stats.win_rate is not None else UNKNOWN
        )
        console.print(f"\n[bold]E = {e_text}R  ·  win rate {wr_text}[/bold]")
        if stats.trades:
            rs = [float(o.r_multiple) for o in result.outcomes if o.valid]
            if len(rs) > 1:
                se = _stats.stdev(rs) / len(rs) ** 0.5
                console.print(
                    f"standard error of E: {se:.3f}R  ·  "
                    f"E/SE = {float(stats.expectancy_r) / se:+.2f}"
                )
                payload["SE"] = se
        if result.rejections:
            console.print("\nRisk Manager rejections: " + ", ".join(
                f"{c} {n}" for c, n in sorted(result.rejections.items(), key=lambda kv: -kv[1])
            ))

        # 6. Slippage sweep (DEC-007). Mandatory:
        # a strategy alive only at 0.02% rests on an execution assumption
        # that we will not have on a $70 position.
        console.print("\n[bold]6. COSTS[/bold]")
        sweep_rows = []
        for level in settings.costs.slippage_sensitivity:
            run_at = await runner(
                settings, start, oos_start, slippage_pct=Decimal(str(level))
            )
            at_stats = summarize(run_at.outcomes)
            e_value = (
                float(at_stats.expectancy_r)
                if at_stats.expectancy_r is not None
                else None
            )
            sweep_rows.append({"slippage_pct": level,
                               "trades": at_stats.trades, "E_R": e_value})
            verdict = "—" if e_value is None else ("alive" if e_value > 0 else "DEAD")
            console.print(
                f"  {level:>5.2f}%  trades {at_stats.trades:>5}  "
                f"E {e_value:+.3f}R  {verdict}"
                if e_value is not None
                else f"  {level:>5.2f}%  trades {at_stats.trades:>5}  E —"
            )
        payload["costs"] = sweep_rows
        alive = [r for r in sweep_rows
                 if r["slippage_pct"] >= 0.10 and (r["E_R"] or 0) > 0]
        console.print(
            "  acceptance threshold \"survives 0.10%\": "
            + ("[bold green]passes[/bold green]" if alive
               else "[bold red]FAIL[/bold red]")
        )

        # 7. Portfolio run. Per-symbol E describes the population
        # of signals; the account lives on the subsample that the limits
        # carved out of it. Without this block "there is an edge" does not translate
        # into "the account grew".
        console.print("\n[bold]7. PORTFOLIO[/bold]")
        from src.backtest.portfolio import replay
        from src.backtest.runner import load_instrument_params
        from src.risk.correlation import CorrelationSource
        from src.risk.governor import GovernorLimits

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
            max_position_pct=(
                Decimal(str(settings.risk.max_position_pct))
                if settings.risk.max_position_pct is not None
                else None
            ),
        )
        book_stats = summarize(book.taken)
        # The acceptance threshold "drawdown p95 < 40%" is computed over PERMUTATIONS
        # of the order, not over the realised path: one path answers
        # "how it turned out", permutations — "how lucky we were".
        from src.backtest.report import monte_carlo_drawdown

        shuffled = monte_carlo_drawdown(
            book.taken,
            initial=Decimal(str(settings.risk.equity_usd)),
            risk_pct=settings.risk.max_loss_per_trade_pct,
        )
        if shuffled is None:
            dd_text = "p95 over permutations not computed"
        elif shuffled < 40:
            dd_text = f"p95 over permutations: {shuffled:.1f}% (40% threshold passed)"
        else:
            dd_text = (
                f"[bold red]p95 over permutations: {shuffled:.1f}% "
                "— FAIL of the 40% threshold[/bold red]"
            )
        console.print(
            f"  candidates {book.candidates}, taken {len(book.taken)} "
            f"({book.taken_share:.1%})\n"
            f"  equity ${book.initial_equity} → ${book.equity:.2f} "
            f"({book.total_return_pct:+.1f}%)\n"
            f"  realised drawdown: {book.max_drawdown_pct:.1f}%  ·  "
            f"{dd_text}\n"
            f"  E of taken: "
            + (
                f"{float(book_stats.expectancy_r):+.3f}R"
                if book_stats.expectancy_r is not None
                else UNKNOWN
            )
            + f"  ·  mode at the end: {book.final_mode.value}"
        )
        if book.rejections:
            console.print("  what cut the frequency:")
            for code, n in sorted(book.rejections.items(), key=lambda kv: -kv[1]):
                console.print(f"    {code:<22}{n:>6}")
        payload["portfolio"] = {
            "candidates": book.candidates,
            "taken": len(book.taken),
            "share": book.taken_share,
            "equity": float(book.equity),
            "return_pct": float(book.total_return_pct),
            "drawdown_pct": float(book.max_drawdown_pct),
            "drawdown_p95": shuffled,
            "E_taken": float(book_stats.expectancy_r)
            if book_stats.expectancy_r is not None
            else None,
            "rejections": book.rejections,
            "mode": book.final_mode.value,
        }

        # 8. Null hypothesis: does the trigger do anything at all.
        #
        # An ATR stop, a 2R target and a holding window can produce
        # positive E by themselves — the distribution of moves is
        # asymmetric. Exactly one detail is replaced here: the entry
        # condition becomes a coin flip, everything else stays the same.
        if name == "c":
            from src.backtest.benchmark import advantage_over, long_share
            from src.backtest.runner import random_entry_benchmark

            console.print("\n[bold]8. RANDOM ENTRY (null hypothesis)[/bold]")
            seen = 0
            for symbol in result.per_symbol:
                seen += len(await load_bars(symbol, timeframe, start, oos_start))
            rate = len(result.outcomes) / seen if seen else 0.0
            share = long_share(result.outcomes)
            bench = await random_entry_benchmark(
                settings, start, oos_start,
                signal_rate=rate, long_share=share,
                breakeven_at_r=Decimal(
                    str(settings.strategy["management"]["breakeven_at_r"])
                ),
            )
            noise = [
                float(o.r_multiple)
                for r in bench.values()
                for o in r.outcomes
                if o.valid and o.r_multiple is not None
            ]
            mine = [
                float(o.r_multiple)
                for o in result.outcomes
                if o.valid and o.r_multiple is not None
            ]
            verdict = advantage_over(mine, noise)
            console.print(
                f"  random trades {len(noise)} over {len(bench)} seeds, "
                f"entry rate {rate:.4f} per bar, longs {share:.0%}"
            )
            if verdict is None:
                console.print("  [dim]nothing to compare[/dim]")
            else:
                e_setup, e_random, z = verdict
                console.print(
                    f"  random entry:   E {e_random:+.3f}R\n"
                    f"  Setup:          E {e_setup:+.3f}R\n"
                    f"  excess: [bold]{z:+.2f}[/bold] standard errors"
                )
                payload["null_hypothesis"] = {
                    "E_random": e_random,
                    "E_setup": e_setup,
                    "z": z,
                    "random_trades": len(noise),
                }

        # 9. Acceptance thresholds — PRELIMINARY, on in-sample.
        #
        # The threshold "E > +0.15R" is defined on out-of-sample, and therefore
        # had never been compared with anything. This was a hole in the report:
        # "all thresholds passed" was written about the thresholds that were computed,
        # while the first of five was silently missing (DEC-028). Here all five
        # are always printed — assuming that out-of-sample
        # repeats in-sample. This is the BEST realistic case, not the
        # expected one: out of sample the result is usually worse.
        from src.backtest.report import (
            SlippageSweep,
            evaluate_thresholds,
            monte_carlo_drawdown,
        )

        console.print(
            "\n[bold]9. ACCEPTANCE THRESHOLDS — preliminary[/bold]\n"
            "[dim]assuming out-of-sample repeats in-sample; "
            "this is the best realistic case, not the expected one[/dim]"
        )
        sweep_obj = SlippageSweep(
            rows=[(r["slippage_pct"], r["trades"], r["E_R"]) for r in sweep_rows]
        )
        population_dd = monte_carlo_drawdown(
            result.outcomes,
            initial=Decimal(str(settings.risk.equity_usd)),
            risk_pct=settings.risk.max_loss_per_trade_pct,
        )
        expected_oos = int(
            stats.trades
            * settings.backtest.out_of_sample_months
            / settings.backtest.in_sample_months
        )
        oos_like = replace_stats_trades(stats, expected_oos)
        acceptance = evaluate_thresholds(
            oos_like, population_dd, sweep_obj, base_e, without
        )
        failed_any = False
        for threshold in acceptance.thresholds:
            mark_ = {True: "[green]PASS[/green]", False: "[bold red]FAIL[/bold red]",
                     None: "not computed"}[threshold.passed]
            failed_any = failed_any or threshold.passed is False
            console.print(
                f"  {threshold.name:<40} {threshold.required:<24} "
                f"{threshold.actual:<40} {mark_}"
            )
        console.print(
            "  [bold red]VERDICT: the Setup will NOT PASS acceptance, even if "
            "out-of-sample repeats in-sample[/bold red]"
            if failed_any
            else "  verdict: will pass if out-of-sample repeats in-sample"
        )
        payload["thresholds_preliminary"] = [
            {"threshold": th.name, "required": th.required,
             "actual": th.actual, "verdict": th.verdict}
            for th in acceptance.thresholds
        ]

        target_path = export or Path(f"var/reports/insample_{name}.json")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        console.print(f"\nRaw numbers: [bold]{target_path}[/bold]")

        await dispose_engine()

    asyncio.run(run())


def replace_stats_trades(stats, trades: int):  # noqa: ANN001, ANN201
    """Statistics with a substituted trade count — for a preliminary
    check of the "trades on OOS" threshold: the expected number of trades on OOS
    at the same intensity as on in-sample."""
    from dataclasses import replace as _replace

    return _replace(stats, trades=trades)


async def _setup_runner(settings, name: str):  # noqa: ANN001, ANN201
    """Runner, parameters and title by the Setup letter.

    For Setup C the break-even stop move is passed EXPLICITLY, not as a
    default value: enabling it changes the result, and that must
    be visible at the call site, not hidden in the signature.
    """
    import functools

    from src.backtest.runner import run_setup_a, run_setup_b, run_setup_c

    if name == "a":
        from src.strategy.setup_a import SetupAParams

        params = SetupAParams.from_config(settings.strategy["setup_a"])
        return run_setup_a, params, f"SETUP A · {settings.strategy['setup_a']['name']}"
    if name == "c":
        from src.strategy.setup_c import SetupCParams

        params = SetupCParams.from_config(settings.strategy["setup_c"])
        runner = functools.partial(
            run_setup_c,
            breakeven_at_r=Decimal(
                str(settings.strategy["management"]["breakeven_at_r"])
            ),
        )
        return runner, params, f"SETUP C · {settings.strategy['setup_c']['name']}"

    from src.strategy.setup_b import SetupBParams

    params = SetupBParams.from_config(settings.strategy["setup_b"])
    return run_setup_b, params, f"SETUP B · {settings.strategy['setup_b']['name']}"



@app.command("validate-setup")
def validate_setup_cmd(
    period: str = typer.Option(..., help="Window from the Setup's plan"),
    setup: str = typer.Option("c", help="Setup: c, d or e"),
) -> None:
    """Setup verdict on a preregistered window. DEC-028, DEC-030, DEC-034.

    Four prohibitions that cannot be bypassed by forgetfulness: trading
    (the Setup's config and code) must match the frozen commit;
    a window — only after the previous ones passed; each window of a Setup —
    once; the out-of-sample window — once for ALL Setups.
    """
    import yaml

    from src.backtest.validation import (
        Ledger,
        code_drift,
        config_at,
        frozen_drift,
        record,
        refusal,
    )

    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    key = f"setup_{setup.lower()}"
    plan = settings.backtest.validation.get(key)
    if plan is None:
        raise typer.BadParameter(f"the config has no validation plan for {key}")

    try:
        frozen = config_at(plan.frozen_at_commit)
        drift = frozen_drift(
            frozen,
            yaml.safe_load(Path("config/settings.yaml").read_text(encoding="utf-8")),
            key,
        )
        drift += code_drift(
            plan.frozen_at_commit, setup=key, extra=tuple(plan.extra_code)
        )
    except ValueError as exc:
        # P-001: could not compare — not "matched".
        console.print(f"[bold red]REFUSED:[/bold red] {exc}")
        raise typer.Exit(code=EXIT_FAILED) from exc
    if drift:
        console.print(
            f"[bold red]REFUSED:[/bold red] trading of {key} has changed "
            f"since the frozen commit {plan.frozen_at_commit}: {', '.join(drift)}. "
            "This is a different hypothesis now — it must be preregistered again."
        )
        raise typer.Exit(code=EXIT_FAILED)

    ledger = Ledger.load()
    reason = refusal(period, plan.frozen_at_commit, ledger, setup=key, plan=plan)
    if reason:
        console.print(f"[bold red]REFUSED:[/bold red] {reason}")
        raise typer.Exit(code=EXIT_FAILED)

    window = plan.windows[period]

    async def run() -> None:
        from src.backtest.evaluation import evaluate
        from src.backtest.report import UNKNOWN

        start = datetime.fromisoformat(window.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(window.end).replace(tzinfo=UTC)

        console.print(
            f"\n[bold]VALIDATION {key.upper()} · {period} ({window.kind})[/bold]\n"
            f"window {window.start} — {window.end}  ·  "
            f"configuration {plan.frozen_at_commit}  ·  "
            f"drawdown by {plan.drawdown_basis}"
        )

        ev = await evaluate(
            settings, key, start, end,
            drawdown_basis=plan.drawdown_basis,
            with_null=plan.null_advantage_min_se is not None,
        )

        def pct(value: float | None) -> str:
            return "—" if value is None else f"{value:.1f}%"

        console.print(
            f"\ntrades {ev.stats.trades}, symbols {ev.result.symbols_traded}  ·  "
            f"exits: {ev.breakdown.describe()}"
        )
        if ev.stats.expectancy_r is not None and ev.se:
            e = float(ev.stats.expectancy_r)
            console.print(f"E {e:+.3f}R, SE {ev.se:.3f}, E/SE {e / ev.se:+.2f}")
        console.print(
            f"portfolio: ${ev.book.initial_equity} → ${ev.book.equity:.2f} "
            f"({ev.book.total_return_pct:+.1f}%), taken {len(ev.book.taken)} "
            f"of {ev.book.candidates}"
        )
        console.print(
            f"drawdown p95: portfolio {pct(ev.dd_portfolio)}, population "
            f"{pct(ev.dd_population)} (counted — {plan.drawdown_basis})\n"
        )

        rows: list[tuple[str, str, str, bool | None]] = [
            (th.name, th.required, th.actual, th.passed) for th in ev.acceptance.thresholds
        ]
        if plan.null_advantage_min_se is not None:
            need = plan.null_advantage_min_se
            if ev.null is None:
                rows.append(("Excess over the null hypothesis", f">= {need} SE", UNKNOWN, None))
            else:
                rows.append((
                    "Excess over the null hypothesis",
                    f">= {need} SE",
                    f"{ev.null.z:+.2f} SE ({ev.null.e_setup:+.3f} vs "
                    f"{ev.null.e_null:+.3f}R, {ev.null.null_trades} trades)",
                    ev.null.z >= need,
                ))
        if plan.time_exit_move_floor:
            floor = float(settings.strategy["min_move_pct"])
            move = ev.median_move_pct
            rows.append((
                "Median move over the holding period",
                f">= {floor}%",
                "—" if move is None else f"{move:.2f}%",
                None if move is None else move >= floor,
            ))

        for name, required, actual, ok in rows:
            verdict = {True: "[green]PASS[/green]", False: "[bold red]FAIL[/bold red]",
                       None: "[yellow]not computed[/yellow]"}[ok]
            console.print(f"  {name:<40} {required:<24} {actual:<48} {verdict}")

        # "Not computed" is not "passed" (P-002).
        passed = all(ok is True for *_, ok in rows)
        console.print(
            "\n[bold green]VERDICT: window passed[/bold green]"
            if passed
            else "\n[bold red]VERDICT: window FAILED[/bold red]"
        )

        record(
            ledger,
            period=period,
            commit=plan.frozen_at_commit,
            window=(window.start, window.end),
            passed=passed,
            thresholds=[
                {"threshold": n, "required": r, "actual": a,
                 "verdict": {True: "PASS", False: "FAIL", None: UNKNOWN}[ok]}
                for n, r, a, ok in rows
            ],
            extra={
                "trades": ev.stats.trades,
                "E_R": float(ev.stats.expectancy_r)
                if ev.stats.expectancy_r is not None else None,
                "portfolio_equity": float(ev.book.equity),
                "drawdown_portfolio": ev.dd_portfolio,
                "drawdown_population": ev.dd_population,
                "best_symbols": ev.top,
                "null_z": None if ev.null is None else ev.null.z,
            },
            setup=key,
            kind=window.kind,
        )
        ledger.save()
        console.print(
            "[dim]result written to config/validation_ledger.json — "
            "commit it: a rerun of this window will be refused[/dim]"
        )

        await dispose_engine()
        if not passed:
            raise typer.Exit(code=EXIT_FAILED)

    asyncio.run(run())


@app.command("watchdog")
def watchdog(
    max_intervals: float = typer.Option(3.0, help="Lag threshold in stream intervals"),
) -> None:
    """Collector liveness check.

    Answers "is there no data or has the collector stopped". Without it these two
    states are indistinguishable, and a failure looks like silence.
    """
    env = get_env()
    configure_logging(env.log_level, env.log_format)

    async def run() -> None:
        stale = await find_stale(STREAM_INTERVALS, max_intervals=max_intervals)
        await dispose_engine()

        if not stale:
            console.print("\n[green]All streams are updating normally.[/green]\n")
            return

        table = Table(header_style="bold red", title="STREAMS NOT UPDATING")
        table.add_column("symbol")
        table.add_column("stream")
        table.add_column("last success")
        table.add_column("lag", justify="right")
        table.add_column("failures", justify="right")
        table.add_column("error")

        for s in stale:
            table.add_row(
                s.symbol,
                s.stream.value,
                "[red]never[/red]" if s.never_succeeded else s.last_success_at.isoformat(),
                "∞" if s.never_succeeded else f"{s.intervals_missed:.1f}×",
                str(s.consecutive_failures),
                (s.last_error or "")[:60],
            )

        console.print(table)
        console.print(
            f"\n[bold red]Stale streams: {len(stale)}[/bold red]  "
            f"(threshold {max_intervals}× interval)\n"
        )
        raise typer.Exit(code=1)

    asyncio.run(run())


#: The cycle is off and waits for a human: reconciliation failed or a limit was hit.
EXIT_HALTED = 4

LIVE_SETUPS = {"d": "setup_d", "e": "setup_e"}


def _live_setup(letter: str) -> str:
    setup = LIVE_SETUPS.get(letter.lower())
    if setup is None:
        raise typer.BadParameter(f"the live cycle exists for Setups {sorted(LIVE_SETUPS)}")
    return setup


@app.command("trade-cycle")
def trade_cycle_cmd(
    setup: str = typer.Option("e", help="Setup: d or e"),
    venue: str = typer.Option(
        "paper",
        help="paper — paper trading on main-market prices (no keys); "
        "testnet — Bybit testnet (testnet keys in .env)",
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Act. Without the flag — only show the decisions"
    ),
) -> None:
    """Daily cycle: reconciliation → exits → entries. Run after 00:00 UTC
    and after backfill --symbols universe-now.

    The main exchange is unreachable by construction: the trading client
    accepts only the testnet address.
    """
    from src.live import report as journal
    from src.live.runner import DataNotFresh, book_of, run_daily

    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)
    name = _live_setup(setup)
    if venue not in {"paper", "testnet"}:
        raise typer.BadParameter("execution venue: paper or testnet")

    async def run():  # noqa: ANN202
        try:
            return await run_daily(settings, name, venue_kind=venue, execute=execute)
        finally:
            await dispose_engine()

    from src.live.lock import BookBusy

    book = book_of(name, venue)
    started = datetime.now(UTC)
    try:
        prepared, report, events = asyncio.run(run())
    except BookBusy as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=EXIT_FAILED) from exc
    except DataNotFresh as exc:
        console.print(f"[bold red]Decision impossible:[/bold red] {exc}")
        journal.append(journal.failure(book=book, now=started, reason=str(exc)))
        raise typer.Exit(code=EXIT_FAILED) from exc
    except Exception as exc:
        journal.append(journal.failure(
            book=book, now=started, reason=f"{type(exc).__name__}: {exc}"
        ))
        raise
    journal.append(journal.render(
        book=book, now=started, execute=execute, universe=len(prepared.universe),
        signals=len(prepared.candidates), skips=prepared.skips, report=report, events=events,
        as_of=prepared.as_of, problem=prepared.problem, note=prepared.note,
    ))
    if prepared.note:
        console.print(f"[yellow]{prepared.note}[/yellow]")
    if prepared.problem:
        console.print(f"[bold red]{prepared.problem}[/bold red]")
    if prepared.as_of is not None and prepared.as_of < started:
        console.print(
            f"[yellow]Run later than scheduled: paper executed the decision at prices as of "
            f"{prepared.as_of:%H:%M} UTC.[/yellow]"
        )

    mode = (
        "[green]EXECUTION[/green]"
        if execute
        else "[yellow]PREVIEW (--execute not set)[/yellow]"
    )
    console.print(
        f"\n[bold]{name} · {venue}[/bold] · decision at {report.moment:%Y-%m-%d %H:%M} UTC · {mode}"
    )
    stale = sorted(s for s, k in prepared.skips.items() if k.value == "STALE_DATA")
    console.print(
        f"Universe: {len(prepared.universe)} · signals: {len(prepared.candidates)} · "
        f"stale data: {len(stale)}"
        + (f" [red]({', '.join(stale[:8])})[/red]" if stale else "")
    )
    for event in events:
        console.print(f"  [dim]{event}[/dim]")
    for symbol, reason, pnl in report.closed:
        console.print(f"  closed {symbol:<14} {reason:<10} {pnl:+.4f} $")
    for symbol in report.opened:
        console.print(f"  [green]opened[/green] {symbol}")
    for symbol, code in report.skipped:
        console.print(f"  [dim]skip {symbol:<14} {code}[/dim]")
    for note in report.notes:
        console.print(f"  [dim]{note}[/dim]")
    console.print(f"Equity: [bold]{report.equity:.2f} $[/bold] · mode {report.mode.value}")
    if report.halted:
        console.print("[bold red]Trading is off — needs a human (trade-status).[/bold red]")
        raise typer.Exit(code=EXIT_HALTED)
    # No universe — a failure, not a partial success: only a retry lets
    # the night script build the month once more on the same day (fixes
    # review 2026-09-28). Exits are already done, a retry is safe.
    if prepared.problem or (stale and len(stale) == len(prepared.universe)):
        raise typer.Exit(code=EXIT_FAILED)
    if stale:
        raise typer.Exit(code=EXIT_PARTIAL)


@app.command("trade-status")
def trade_status_cmd(
    setup: str = typer.Option("e", help="Setup: d or e"),
    venue: str = typer.Option("paper", help="paper or testnet"),
    journal: int = typer.Option(15, help="How many latest journal entries to show"),
) -> None:
    """Book state: mode, equity, open and closed trades, journal."""
    from sqlalchemy import select as _select

    from src.core.db import LiveJournal, LivePosition, session_scope
    from src.live.runner import book_of
    from src.live.store import DbStore

    book = book_of(_live_setup(setup), venue)

    async def run() -> None:
        state = await DbStore().load_state(book)
        async with session_scope() as s:
            trades = (
                await s.execute(
                    _select(LivePosition).where(LivePosition.setup == book)
                    .order_by(LivePosition.id)
                )
            ).scalars().all()
            notes = (
                await s.execute(
                    _select(LiveJournal).where(LiveJournal.setup == book)
                    .order_by(LiveJournal.id.desc()).limit(journal)
                )
            ).scalars().all()
        await dispose_engine()

        if state is None:
            console.print(
                f"[yellow]{book}: book is empty — the cycle has not run with --execute[/yellow]"
            )
        else:
            console.print(
                f"[bold]{book}[/bold] · mode {state.mode.value} · equity {state.equity:.2f} $ · "
                f"day from {state.anchors.day_equity:.2f} · "
                f"week from {state.anchors.week_equity:.2f}"
            )
        closed = [t for t in trades if t.status == "closed" and t.pnl is not None]
        if closed:
            wins = sum(1 for t in closed if t.pnl > 0)
            total = sum((t.pnl for t in closed), Decimal(0))
            console.print(
                f"Trades closed: {len(closed)} · in profit: {wins} · total {total:+.2f} $"
            )
        table = Table(title="Trades")
        for col in ("symbol", "side", "qty", "entry", "stop", "until", "status", "result $"):
            table.add_column(col)
        for t in trades[-20:]:
            table.add_row(
                t.symbol, "long" if t.is_long else "short", f"{t.qty.normalize()}",
                f"{t.entry_price.normalize()}", f"{t.stop_price.normalize()}",
                f"{t.close_due_at:%m-%d}",
                t.status + (f" {t.close_reason}" if t.close_reason else ""),
                f"{t.pnl:+.4f}" if t.pnl is not None else "",
            )
        console.print(table)
        for n in reversed(notes):
            console.print(
                f"  [dim]{n.ts:%m-%d %H:%M} {n.action:<16} {n.symbol or '':<14} "
                f"{n.code or '':<18} {n.note}[/dim]"
            )

    asyncio.run(run())


@app.command("trade-resume")
def trade_resume_cmd(
    setup: str = typer.Option("e", help="Setup: d or e"),
    venue: str = typer.Option("paper", help="paper or testnet"),
    reason: str = typer.Option(..., help="Why we switch on: goes into the journal"),
) -> None:
    """Manual switch-on after a switch-off (weekly limit, mismatch).

    Loss anchors are moved to the current equity — otherwise the system
    would switch on and immediately switch off on the old loss.
    """
    from src.live.lock import BookBusy, book_lock
    from src.live.runner import book_of
    from src.live.store import DbStore, JournalEntry

    book = book_of(_live_setup(setup), venue)

    async def run() -> None:
        # Under the book lock and in one transaction: mode and anchors
        # change, equity does not. Previously the row was overwritten entirely,
        # and a switch-on during the cycle could bring back stale equity
        # (bot platform design 2026-09-29, DEC-049).
        store = DbStore()
        now = datetime.now(UTC)
        try:
            previous = await store.resume(book, now)
            if previous is None:
                console.print(f"[yellow]{book}: no book — nothing to switch on[/yellow]")
                return
            await store.journal(JournalEntry(ts=now, setup=book, action="RESUME",
                                             code=previous.value, note=reason, dry_run=False))
            console.print(f"{book}: {previous.value} → ON")
        finally:
            await dispose_engine()

    try:
        with book_lock(book):
            asyncio.run(run())
    except BookBusy as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        raise typer.Exit(code=EXIT_FAILED) from exc


@app.command("testnet-check")
def testnet_check_cmd(
    roundtrip: str = typer.Option(
        "", help="Symbol for a full order round trip at minimum size (e.g. XRPUSDT)"
    ),
) -> None:
    """Bybit testnet connection check: clock, key, permissions, account,
    position mode; with --roundtrip — entry, stop, close on test money."""
    from src.exchange.trading_client import BybitTradingClient
    from src.live.testnet_check import run_check

    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)
    try:
        client = BybitTradingClient(
            settings.bybit, api_key=env.bybit_api_key, api_secret=env.bybit_api_secret
        )
    except ValueError as exc:
        console.print(f"[bold red]No keys:[/bold red] {exc}")
        raise typer.Exit(code=EXIT_FAILED) from exc

    async def run():  # noqa: ANN202
        async with client:
            return await run_check(client, roundtrip_symbol=roundtrip.upper() or None)

    report = asyncio.run(run())
    for step in report.steps:
        mark = (
            "[green]✓[/green]" if step.ok
            else "[yellow]![/yellow]" if step.warn else "[red]✕[/red]"
        )
        console.print(f"  {mark} {step.name:<18} {step.detail}")
    if report.ok:
        console.print("[bold green]testnet ready[/bold green]")
    else:
        raise typer.Exit(code=EXIT_FAILED)


@app.command("paper-monitor")
def paper_monitor_cmd(
    setup: str = typer.Option("e", help="Setup: d or e"),
) -> None:
    """Day watch over paper: triggered stops are booked immediately,
    no new decisions. Only after the night's day's decision. Run after
    backfill --symbols held --streams kline_1h,funding."""
    from src.live import report as journal
    from src.live.runner import book_of, monitor_paper

    settings = get_settings()
    env = get_env()
    configure_logging(env.log_level, env.log_format)
    name = _live_setup(setup)

    async def run():  # noqa: ANN202
        try:
            return await monitor_paper(settings, name)
        finally:
            await dispose_engine()

    from src.live.lock import BookBusy

    started = datetime.now(UTC)
    try:
        result = asyncio.run(run())
    except BookBusy as exc:
        # Busy with the night cycle or a manual run — not a day watch failure.
        console.print(f"day watch: {exc}")
        return
    if result.idle:
        console.print(f"day watch: {result.idle}")
        return
    rep = result.report
    for event in result.events:
        console.print(f"  {event}")
    for symbol, reason, pnl in rep.closed:
        console.print(f"  closed {symbol} {reason} {pnl:+.4f} $")
    console.print(f"day watch: equity {rep.equity:.2f} $, mode {rep.mode.value}")
    # Into the human-readable journal — only when something happened.
    if rep.closed or rep.halted or result.events:
        journal.append(journal.monitor_block(
            book=book_of(name, "paper"), now=started, report=rep, events=result.events
        ))
    if rep.halted:
        raise typer.Exit(code=EXIT_HALTED)


@app.command("web")
def web_cmd(
    port: int = typer.Option(8765, help="Panel port"),
) -> None:
    """Monitoring panel: http://127.0.0.1:8765. Read-only, localhost only."""
    import uvicorn

    uvicorn.run("src.web.app:app", host="127.0.0.1", port=port, log_level="warning")


def _make_collector(client: BybitClient, stream: str, funding_interval: int):
    """Builds a collector by stream name."""
    if stream.startswith("kline_"):
        return OhlcvCollector(client, stream.removeprefix("kline_"))
    if stream == "oi":
        return OpenInterestCollector(client, "1h")
    if stream == "funding":
        return FundingCollector(client, funding_interval)
    # P-001: only what passed the validation above gets here;
    # if something else did — it is a code bug, not an input error.
    raise ValueError(f"no collector for stream {stream!r}")


if __name__ == "__main__":
    app()
