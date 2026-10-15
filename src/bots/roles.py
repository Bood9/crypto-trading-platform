"""Bot platform roles. DEC-049.

Each role owns its own area of responsibility, and its result is checked
by the next one:

* **Collector** — data. Its manifest (freshness per symbol, via raw SQL)
  is shown on the panel and re-checked by the Controller.
* **Analyst** — signals at the decision moment, same code as the backtest.
* **Risk Manager** — rehearsal of the decision on a copy of the book and
  venue (writes nothing) → approval or rejection with a code for EVERY
  candidate. Recomputes the signals itself and must get exactly what the
  Analyst got.
* **Executor** — the only one who writes to the book and the venue. Enters
  only with two "yes": its own decision and the Risk Manager's approval
  (matching side, and on paper also qty, price and stop). Exits and stops —
  always, even without approvals.
* **Controller** — independent checks: data via raw SQL, signals on the
  full history, two keys, accounting, paper marks, position protection.
  For the first 14 nights it only reports: a false alarm must not stop
  trading (design judges).

A role gets everything through `RoleContext` and returns a `RoleResult`;
`run_role` writes the run to the board. This way roles are tested without a DB.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from src.bots.board import Board, Intent, Trace
from src.core.config import Settings
from src.live.cycle import EntryGate, PlannedEntry
from src.live.lock import BookBusy
from src.live.signals import Skip, decision_moment
from src.live.store import DbStore, MemoryStore, ReadOnlyStore, Store

LOGS = Path("var/live")

#: Same codes as trade-cycle (src/cli.py): the schedule
#: (scripts/scheduled.sh) understands only these.
EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_HALTED = 0, 1, 3, 4
#: build-universe --check: time to build the month.
EXIT_UNIVERSE_DUE = 5

#: Controller tolerance when recomputing the stop on the full history: ATR forgets
#: the start as (13/14)^n, after 400 days the trace is ~4e-13 (LIVE_HISTORY_DAYS).
STOP_TOLERANCE = Decimal("1e-6")

#: Second-key tolerance on paper: the DB stores 18 decimal places, while the
#: plan may carry more. A bigger mismatch is a defect, not rounding.
GATE_TOLERANCE = Decimal("1e-9")

#: How many nights the Controller only reports (design judges).
SHADOW_NIGHTS = 14

EPOCH = datetime(2019, 1, 1, tzinfo=UTC)


@dataclass
class RoleContext:
    settings: Settings
    board: Board
    setup: str
    venue: str
    shift: str  # night / day
    #: One "now" for the whole shift: the Risk Manager and the Executor must
    #: decide at the same moment, otherwise paper has different prices.
    now: datetime
    execute: bool = True
    #: Book store. None — the DB; in tests — MemoryStore.
    store: Store | None = None
    #: Runs a CLI command (for the Collector) — replaced in tests.
    run_cli: Callable[[list[str], Path], Awaitable[int]] | None = None

    @property
    def book(self) -> str:
        return f"{self.setup}:{self.venue}"

    @property
    def board_book(self) -> str:
        """Book on the board. The preview (--dry) writes under its own: otherwise
        it would replace the day's real approvals and become the "last run"
        on the panel (core review 2026-09-30)."""
        return self.book if self.execute else f"{self.book}:dry"

    @property
    def moment(self) -> datetime:
        return decision_moment(self.now)

    @property
    def uses_db(self) -> bool:
        return self.store is None

    def book_store(self) -> Store:
        return self.store or DbStore()


@dataclass
class RoleResult:
    status: str  # ok / refused / failed / skipped
    summary: str
    outputs: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    traces: list[Trace] = field(default_factory=list)
    #: The day's approvals (Risk Manager only). None — leave the board alone.
    intents: list[Intent] | None = None
    exit_code: int = EXIT_OK
    error: str | None = None


def _s(value) -> str | None:  # noqa: ANN001
    return None if value is None else format(Decimal(value).normalize(), "f")


def _side(is_long: bool) -> str:
    return "long" if is_long else "short"


# ── Collector ───────────────────────────────────────────────
async def _subprocess_cli(args: list[str], log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"##### {datetime.now(UTC):%H:%M:%S} UTC: {' '.join(args)}\n")
        fh.flush()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "src.cli", *args, stdout=fh, stderr=subprocess.STDOUT
        )
        return await proc.wait()


UNIVERSE_DATA = ["backfill", "--symbols", "universe-now", "--streams",
                 "kline_4h,kline_1h,funding", "--years", "1.2", "--concurrency", "4"]


async def collector(ctx: RoleContext) -> RoleResult:
    """Same steps as the top half of daily.sh — with a result for each."""
    run = ctx.run_cli or _subprocess_cli
    stamp = f"{ctx.now:%Y%m%d}"
    steps: list[dict] = []

    async def step(name: str, args: list[str], log: str) -> int:
        started = time.monotonic()
        code = await run(args, LOGS / f"{log}_{stamp}.log")
        steps.append({"step": name, "code": code,
                      "seconds": round(time.monotonic() - started, 1)})
        return code

    if ctx.shift == "day":
        await step("hourly bars and funding of open positions",
                   ["backfill", "--symbols", "held", "--streams", "kline_1h,funding",
                    "--years", "0.05"], "backfill")
    else:
        await step("universe and open-position data", UNIVERSE_DATA, "backfill")
        due = await step("is it time to build a new month",
                         ["build-universe", "--month", "due", "--check"], "universe")
        if due == EXIT_UNIVERSE_DUE:
            # A full collection failure (1) — nothing to build from; a partial
            # one (3) is acceptable: build-universe checks month completeness itself.
            got = await step("4h for all candidates of the month",
                             ["backfill", "--symbols", "candidates", "--streams", "kline_4h",
                              "--years", "1.2", "--concurrency", "4"], "universe")
            if got != EXIT_FAILED:
                built = await step("building the month's universe",
                                   ["build-universe", "--month", "due"], "universe")
                if built == EXIT_OK:
                    await step("data for the month's new symbols", UNIVERSE_DATA, "backfill")

    manifest = await _manifest(ctx) if ctx.uses_db else {}
    failed = [s for s in steps if s["code"] not in (EXIT_OK, EXIT_PARTIAL, EXIT_UNIVERSE_DUE)]
    partial = [s for s in steps if s["code"] == EXIT_PARTIAL]
    if failed:
        summary = "failure: " + ", ".join(s["step"] for s in failed)
    elif partial:
        summary = f"partial: {len(partial)} step(s) with gaps"
    else:
        summary = "data collected"
    stale = len(manifest.get("stale_4h", [])) + len(manifest.get("stale_1h", []))
    if stale:
        summary += f"; {stale} series lagging"
    return RoleResult(
        # A collection failure does not stop the shift: reconciliation and exits
        # do not depend on signal freshness, and the Analyst skips lagging
        # symbols with code STALE_DATA.
        status="failed" if failed else "ok",
        summary=summary,
        outputs={"steps": steps, "manifest": manifest},
        metrics={"seconds": round(sum(s["seconds"] for s in steps), 1),
                 "steps": len(steps), "failed_steps": len(failed),
                 "symbols": manifest.get("symbols", 0),
                 "stale_4h": len(manifest.get("stale_4h", [])),
                 "stale_1h": len(manifest.get("stale_1h", []))},
        exit_code=EXIT_FAILED if failed else EXIT_OK,
    )


async def _last_bars(symbols: list[str]) -> dict[tuple[str, str], datetime]:
    from sqlalchemy import func, select

    from src.core.db import Ohlcv, session_scope

    if not symbols:
        return {}
    async with session_scope() as s:
        rows = (await s.execute(
            select(Ohlcv.symbol, Ohlcv.timeframe, func.max(Ohlcv.ts))
            .where(Ohlcv.symbol.in_(symbols), Ohlcv.timeframe.in_(("4h", "1h")))
            .group_by(Ohlcv.symbol, Ohlcv.timeframe)
        )).all()
    return {(sym, tf): ts for sym, tf, ts in rows}


async def _full_days(symbols: list[str], day: datetime) -> set[str]:
    """Symbols that have all six 4h bars of day `day`: the Analyst drops
    incomplete days (to_daily) — and gets STALE_DATA."""
    from sqlalchemy import func, select

    from src.core.db import Ohlcv, session_scope

    if not symbols:
        return set()
    async with session_scope() as s:
        rows = (await s.execute(
            select(Ohlcv.symbol, func.count())
            .where(Ohlcv.symbol.in_(symbols), Ohlcv.timeframe == "4h",
                   Ohlcv.ts >= day, Ohlcv.ts < day + timedelta(days=1))
            .group_by(Ohlcv.symbol)
        )).all()
    return {sym for sym, n in rows if n >= 6}


async def _held(book: str) -> list[str]:
    from sqlalchemy import select

    from src.core.db import LivePosition, session_scope

    async with session_scope() as s:
        return sorted(set((await s.execute(
            select(LivePosition.symbol).where(LivePosition.setup == book,
                                              LivePosition.status.in_(("pending", "open")))
        )).scalars()))


async def _manifest(ctx: RoleContext) -> dict:
    """Freshness of every series — via raw SQL, bypassing the bar loader."""
    from src.live.runner import month_of_decision, universe_of

    universe = await universe_of(month_of_decision(ctx.moment)) if ctx.shift == "night" else []
    held = await _held(ctx.book)
    symbols = sorted(set(universe) | set(held))
    last = await _last_bars(symbols)
    hour = ctx.now.replace(minute=0, second=0, microsecond=0)
    # The last 4h bar of the decision day is 20:00; the hourly one is an hour
    # before the decision at night and before the current hour by day.
    need_4h = ctx.moment - timedelta(hours=4)
    need_1h = (ctx.moment if ctx.shift == "night" else hour) - timedelta(hours=1)
    full = await _full_days(universe, ctx.moment - timedelta(days=1))
    stale_4h = sorted(s for s in universe if s not in full)
    stale_1h = sorted(s for s in symbols if last.get((s, "1h"), EPOCH) < need_1h)
    return {"symbols": len(symbols), "universe": len(universe), "held": held,
            "stale_4h": stale_4h, "stale_1h": stale_1h,
            "need_4h": need_4h.isoformat(), "need_1h": need_1h.isoformat()}


# ── Analyst ─────────────────────────────────────────────────
def _candidate_row(c, queue: int) -> dict:  # noqa: ANN001
    return {"symbol": c.symbol, "side": _side(c.is_long), "close": _s(c.close),
            "stop": _s(c.stop), "bar": c.bar_ts.isoformat(), "queue": queue}


def candidate_rows(candidates) -> list[dict]:  # noqa: ANN001
    return [_candidate_row(c, i + 1) for i, c in enumerate(candidates)]


async def analyst(ctx: RoleContext) -> RoleResult:
    from src.live.runner import prepare

    prepared = await prepare(ctx.settings, ctx.setup, ctx.now)
    if prepared.problem:
        return RoleResult(status="refused", summary=prepared.problem,
                          outputs={"problem": prepared.problem, "candidates": []},
                          metrics={"universe": 0, "candidates": 0})
    rows = candidate_rows(prepared.candidates)
    traces = [Trace(r["symbol"], "signal", "pass", None,
                    {k: r[k] for k in ("side", "close", "stop", "queue")}) for r in rows]
    counts: dict[str, int] = {}
    for symbol, skip in sorted(prepared.skips.items()):
        counts[skip.value] = counts.get(skip.value, 0) + 1
        # "No signal" is the normal outcome for most symbols; the path is
        # tracked only for those that had or could have had a signal.
        if skip is not Skip.NO_SIGNAL:
            traces.append(Trace(symbol, "signal", "reject", skip.value))
    stale = counts.get(Skip.STALE_DATA.value, 0)
    all_stale = bool(prepared.universe) and stale == len(prepared.universe)
    return RoleResult(
        status="refused" if all_stale else "ok",
        summary=(f"signals {len(rows)} of {len(prepared.universe)}"
                 + (f", stale data {stale}" if stale else "")),
        outputs={"universe": len(prepared.universe), "candidates": rows, "skips": counts,
                 "stale": sorted(s for s, k in prepared.skips.items()
                                 if k is Skip.STALE_DATA)},
        metrics={"universe": len(prepared.universe), "candidates": len(rows),
                 "no_signal": counts.get(Skip.NO_SIGNAL.value, 0),
                 "short_history": counts.get(Skip.SHORT_HISTORY.value, 0), "stale": stale},
        traces=traces,
    )


# ── Risk Manager ────────────────────────────────────────────
def _rest_code(prepared, report) -> str:  # noqa: ANN001
    """Why the cycle did not reach the candidate."""
    if prepared.note:
        return "DECISION_DONE"
    if report.halted:
        return "HALTED"
    return {"OFF_DAILY": "DAILY_STOP", "OFF_WEEKLY": "WEEKLY_STOP"}.get(
        report.mode.value, "MODE_OFF")


async def risk(ctx: RoleContext) -> RoleResult:
    from src.live.runner import run_daily

    signals = await ctx.board.latest_run("analyst", ctx.board_book, ctx.moment, ctx.shift)
    if signals is None or signals.status != "ok":
        why = signals.summary if signals else "Analyst did not run"
        return RoleResult(status="refused", summary=f"nothing to approve: {why}", intents=[],
                          outputs={"analyst": signals.status if signals else None})
    if ctx.venue == "paper" and ctx.execute:
        # Full cycle on a copy: the paper decision is deterministic, and the plan
        # must match the Executor's plan to the digit. In preview the Executor
        # only looks too — and so does the Risk Manager.
        prepared, report, _ = await run_daily(ctx.settings, ctx.setup, venue_kind=ctx.venue,
                                              execute=True, rehearse=True, now=ctx.now,
                                              store=ctx.book_store())
    else:
        # On a real venue a trial execution is impossible — preview, and the
        # Executor is required to match only the side.
        prepared, report, _ = await run_daily(ctx.settings, ctx.setup, venue_kind=ctx.venue,
                                              execute=False, now=ctx.now,
                                              store=ReadOnlyStore(ctx.book_store()))
    mine = candidate_rows(prepared.candidates)
    theirs = signals.outputs.get("candidates", [])
    if mine != theirs:
        return RoleResult(
            status="refused", summary="signals did not match the Analyst — no approvals",
            intents=[], outputs={"mine": mine, "theirs": theirs},
            traces=[Trace(r["symbol"], "risk", "reject", "SIGNAL_MISMATCH") for r in theirs],
        )
    intents: list[Intent] = []
    traces: list[Trace] = []
    for p in report.planned:
        intents.append(Intent(p.symbol, "approve", None, p.is_long, p.qty, p.entry_price,
                              p.stop_price))
        traces.append(Trace(p.symbol, "risk", "pass", None, {
            "side": _side(p.is_long), "qty": _s(p.qty), "entry": _s(p.entry_price),
            "stop": _s(p.stop_price)}))
    decided = {i.symbol for i in intents}
    for symbol, code in report.skipped:
        intents.append(Intent(symbol, "reject", code))
        traces.append(Trace(symbol, "risk", "reject", code))
        decided.add(symbol)
    # Candidates the cycle did not reach: trading is off, the day's decision
    # is already made, halted by reconciliation.
    rest = _rest_code(prepared, report)
    for r in mine:
        if r["symbol"] not in decided:
            intents.append(Intent(r["symbol"], "reject", rest))
            traces.append(Trace(r["symbol"], "risk", "reject", rest))
    approved = [i.symbol for i in intents if i.verdict == "approve"]
    return RoleResult(
        status="ok", summary=f"approved {len(approved)} of {len(mine)}",
        outputs={"approved": approved,
                 "rejected": {i.symbol: i.code for i in intents if i.verdict == "reject"},
                 "would_close": [[s, r, _s(p)] for s, r, p in report.closed],
                 "equity_after": _s(report.equity), "mode": report.mode.value,
                 "rehearsal": ctx.venue == "paper" and ctx.execute},
        metrics={"candidates": len(mine), "approved": len(approved),
                 "rejected": len(intents) - len(approved)},
        traces=traces, intents=intents,
    )


# ── Executor ────────────────────────────────────────────────
def exit_code_of(prepared, report) -> int:  # noqa: ANN001
    """Cycle code — as in trade-cycle: a halt matters most; no universe or
    all data stale — rejection, the schedule will retry; part of the data
    stale — the day counts."""
    stale = [s for s, k in prepared.skips.items() if k is Skip.STALE_DATA]
    if report.halted:
        return EXIT_HALTED
    if prepared.problem or (stale and len(stale) == len(prepared.universe)):
        return EXIT_FAILED
    if stale:
        return EXIT_PARTIAL
    return EXIT_OK


def _near(a: Decimal | None, b: Decimal) -> bool:
    return a is not None and abs(a - b) <= GATE_TOLERANCE * max(abs(b), Decimal(1))


def make_gate(intents: dict[str, Intent], *, exact: bool) -> EntryGate:
    """The second key. On paper the decision is deterministic — it must match;
    a mismatch there is a defect, not the market."""

    async def gate(p: PlannedEntry) -> str | None:
        i = intents.get(p.symbol)
        if i is None or i.verdict != "approve":
            return "RISK_NOT_APPROVED"
        if i.is_long != p.is_long:
            return "RISK_MISMATCH"
        if exact and not (_near(i.qty, p.qty) and _near(i.entry_price, p.entry_price)
                          and _near(i.stop_price, p.stop_price)):
            return "RISK_MISMATCH"
        return None

    return gate


async def executor(ctx: RoleContext) -> RoleResult:
    if ctx.shift == "day":
        return await _watch(ctx)
    from src.live import report as journal
    from src.live.runner import DataNotFresh, run_daily

    # Trust approvals only if the LAST Risk Manager run of this night gave a
    # legitimate answer: a failed retry would leave the previous attempt's
    # approvals on the board.
    approvals = await ctx.board.latest_run("risk", ctx.board_book, ctx.moment, "night")
    valid = approvals is not None and approvals.status == "ok"
    approved = await ctx.board.intents(ctx.board_book, ctx.moment) if valid else {}
    blocked = None if valid else (
        "Risk Manager gave no approvals ("
        + (approvals.summary if approvals else "did not run")
        + ") — no entries, exits done, day's decision not counted, retry in an hour"
    )
    gate = make_gate(approved, exact=ctx.venue == "paper")
    try:
        # Without legitimate approvals — an attempt without a decision: otherwise a
        # late attempt would move the venue forward, and the retry could not enter.
        prepared, rep, events = await run_daily(ctx.settings, ctx.setup, venue_kind=ctx.venue,
                                                execute=ctx.execute, now=ctx.now,
                                                store=ctx.store, gate=gate, decide=valid)
    except BookBusy as exc:
        return RoleResult(status="failed", summary=str(exc), exit_code=EXIT_FAILED,
                          error=str(exc))
    except DataNotFresh as exc:
        if ctx.uses_db:
            journal.append(journal.failure(book=ctx.book, now=ctx.now, reason=str(exc)))
        return RoleResult(status="failed", summary=f"decision impossible: {exc}",
                          exit_code=EXIT_FAILED, error=str(exc))
    except Exception as exc:
        if ctx.uses_db:
            journal.append(journal.failure(book=ctx.book, now=ctx.now,
                                           reason=f"{type(exc).__name__}: {exc}"))
        raise
    if ctx.uses_db:
        journal.append(journal.render(
            book=ctx.book, now=ctx.now, execute=ctx.execute, universe=len(prepared.universe),
            signals=len(prepared.candidates), skips=prepared.skips, report=rep, events=events,
            as_of=prepared.as_of, problem=prepared.problem or blocked, note=prepared.note,
        ))
    opened = set(rep.opened)
    traces = [Trace(p.symbol, "execution", "pass", None, {
        "side": _side(p.is_long), "qty": _s(p.qty), "entry": _s(p.entry_price),
        "stop": _s(p.stop_price)}) for p in rep.planned if p.symbol in opened]
    traces += [Trace(s, "execution", "reject", code) for s, code in rep.skipped]
    traces += [Trace(s, "execution", "info", reason, {"pnl": _s(pnl)})
               for s, reason, pnl in rep.closed]
    code = exit_code_of(prepared, rep)
    if blocked and code in (EXIT_OK, EXIT_PARTIAL):
        code = EXIT_FAILED
    if code != EXIT_FAILED:
        status = "ok"
    else:
        # Without approvals — a refusal to enter (exits done), not a failure.
        status = "refused" if blocked and not prepared.problem else "failed"
    return RoleResult(
        status=status,
        summary=(prepared.problem or blocked or
                 f"opened {len(rep.opened)}, closed {len(rep.closed)}, "
                 f"equity {rep.equity:.2f}, mode {rep.mode.value}"),
        outputs={"opened": sorted(opened),
                 "closed": [[s, r, _s(p)] for s, r, p in rep.closed],
                 "skipped": dict(rep.skipped), "equity": _s(rep.equity),
                 "mode": rep.mode.value, "halted": rep.halted, "note": prepared.note,
                 "events": events,
                 "approved": sorted(s for s, i in approved.items() if i.verdict == "approve"),
                 "approvals_valid": valid,
                 "as_of": prepared.as_of.isoformat() if prepared.as_of else None},
        metrics={"opened": len(rep.opened), "closed": len(rep.closed),
                 "skipped": len(rep.skipped), "equity": float(rep.equity)},
        traces=traces, exit_code=code,
    )


async def _watch(ctx: RoleContext) -> RoleResult:
    """Executor's day shift: stops are booked immediately, no entries."""
    from src.live import report as journal
    from src.live.runner import monitor_paper

    if ctx.venue != "paper":
        return RoleResult(status="skipped", summary="day watch is for paper only")
    try:
        result = await monitor_paper(ctx.settings, ctx.setup, now=ctx.now, store=ctx.store)
    except BookBusy as exc:
        # Busy with the night cycle or a manual run — not a day watch failure.
        return RoleResult(status="skipped", summary=str(exc))
    if result.idle:
        return RoleResult(status="skipped", summary=result.idle)
    rep = result.report
    if ctx.uses_db and (rep.closed or rep.halted or result.events):
        journal.append(journal.monitor_block(book=ctx.book, now=ctx.now, report=rep,
                                             events=result.events))
    return RoleResult(
        status="ok",
        summary=f"closed {len(rep.closed)}, equity {rep.equity:.2f}, mode {rep.mode.value}",
        outputs={"closed": [[s, r, _s(p)] for s, r, p in rep.closed], "events": result.events,
                 "equity": _s(rep.equity), "mode": rep.mode.value, "halted": rep.halted},
        metrics={"closed": len(rep.closed), "events": len(result.events),
                 "equity": float(rep.equity)},
        traces=[Trace(s, "execution", "info", reason, {"pnl": _s(pnl)})
                for s, reason, pnl in rep.closed],
        exit_code=EXIT_HALTED if rep.halted else EXIT_OK,
    )


# ── Controller ──────────────────────────────────────────────
async def controller(ctx: RoleContext) -> RoleResult:
    """Independent checks. Does not call the sizer, governor or cycle — computes
    by itself, from raw data. For now only reports (SHADOW_NIGHTS)."""
    checks: list[dict] = []
    traces: list[Trace] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    store = ctx.book_store()
    state = await store.load_state(ctx.book)
    trades = await store.live_trades(ctx.book)

    # Accounting: equity = starting equity + sum of closed trade results.
    closed = await _closed_pnl(ctx, store)
    if state is not None and closed is not None:
        expected = Decimal(str(ctx.settings.risk.equity_usd)) + closed
        check("accounting", abs(state.equity - expected) < Decimal("1e-9"),
              f"equity {state.equity:.6f} $, from closed trades {expected:.6f} $")

    if ctx.venue == "paper":
        from src.live.paper_venue import MarketPaperVenue
        from src.live.runner import PAPER_DIR

        venue = MarketPaperVenue.load(PAPER_DIR / f"{ctx.setup}.json")
        seen, decided = venue.seen_until, venue.decided_for
        # A venue that has seen time past the decision moment — only with the
        # day's decision made: otherwise the next attempt would decide
        # "at 00:10" knowing the future (sixth review round).
        ahead = seen is not None and seen > ctx.moment
        check("paper marks", not ahead or (decided is not None and decided >= ctx.moment),
              f"seen until {seen:%d.%m %H:%M}, decision for {decided:%d.%m}"
              if seen and decided else f"seen until {seen}, decision for {decided}")
        held = set(venue.held_symbols)
        resting = {o.link_id for o in await venue.open_orders()}
        for t in trades:
            if t.status != "open":
                continue
            if t.symbol in held:
                ok = t.stop_link_id in resting
                detail = "position and stop on venue" if ok else "POSITION WITHOUT STOP on venue"
            else:
                # A late run moves the venue forward after the cycle: a stop
                # triggered there is booked by the next run (core review).
                ok = await venue.fill(t.stop_link_id, t.symbol) is not None
                detail = ("stop executed on the venue, will be booked by the next run" if ok
                          else "NO position on the venue, and the stop did not execute")
            check(f"protection {t.symbol}", ok, detail)

    if ctx.shift == "night":
        executed = await ctx.board.latest_run("executor", ctx.board_book, ctx.moment, "night")
        intents = await ctx.board.intents(ctx.board_book, ctx.moment)
        opened = executed.outputs.get("opened", []) if executed else []
        unapproved = [s for s in opened
                      if intents.get(s) is None or intents[s].verdict != "approve"]
        check("two keys", not unapproved,
              f"entries {len(opened)}, without approval: {', '.join(unapproved) or 'none'}")
        for s in opened:
            ok = s not in unapproved
            traces.append(Trace(s, "verify", "pass" if ok else "fail",
                                None if ok else "RISK_NOT_APPROVED"))

        signals = await ctx.board.latest_run("analyst", ctx.board_book, ctx.moment, "night")
        if signals and signals.status == "ok" and ctx.uses_db:
            rows = signals.outputs.get("candidates", [])
            diffs = await _recompute_signals(ctx, rows)
            check("signals on full history", not diffs,
                  f"matched ({len(rows)})" if not diffs else "; ".join(diffs[:5]))
            manifest = await _manifest(ctx)
            mine, theirs = set(manifest["stale_4h"]), set(signals.outputs.get("stale", []))
            check("data freshness", mine == theirs,
                  f"lagging by raw data {len(mine)}, per the Analyst {len(theirs)}"
                  + ("" if mine == theirs else f": {sorted(mine ^ theirs)[:8]}"))

    failed = [c for c in checks if not c["ok"]]
    return RoleResult(
        # Only reports for now: a failed check is visible on the panel and in
        # the notification, but does not change the shift code.
        status="failed" if failed else "ok",
        summary=(f"checks {len(checks)}, all passed" if not failed
                 else f"failed {len(failed)} of {len(checks)}: "
                      + "; ".join(c["check"] for c in failed)),
        outputs={"checks": checks, "shadow": True},
        metrics={"checks": len(checks), "failed": len(failed)},
        traces=traces,
    )


async def _closed_pnl(ctx: RoleContext, store: Store) -> Decimal | None:
    if isinstance(store, MemoryStore):
        return sum((t.pnl for t in store.trades
                    if t.setup == ctx.book and t.status == "closed" and t.pnl is not None),
                   Decimal(0))
    if not ctx.uses_db:
        return None
    from sqlalchemy import func, select

    from src.core.db import LivePosition, session_scope

    async with session_scope() as s:
        total = (await s.execute(
            select(func.coalesce(func.sum(LivePosition.pnl), 0))
            .where(LivePosition.setup == ctx.book, LivePosition.status == "closed")
        )).scalar()
    return Decimal(total)


async def _recompute_signals(ctx: RoleContext, rows: list[dict]) -> list[str]:
    """The signal again on the FULL history — as the backtest computes it, not
    on the LIVE_HISTORY_DAYS window the live cycle uses."""
    from src.backtest.runner import load_bars
    from src.live.signals import Candidate, candidate_for
    from src.strategy.setup_d import SetupDParams

    params = SetupDParams.from_config(ctx.settings.strategy[ctx.setup])
    diffs: list[str] = []
    for r in rows:
        bars = await load_bars(r["symbol"], "4h", EPOCH, ctx.moment)
        full = candidate_for(r["symbol"], bars, params, ctx.moment)
        if not isinstance(full, Candidate):
            diffs.append(f"{r['symbol']}: no signal on the full history ({full})")
            continue
        rel = abs(full.stop - Decimal(r["stop"])) / full.close
        if _side(full.is_long) != r["side"] or rel > STOP_TOLERANCE:
            diffs.append(f"{r['symbol']}: {_side(full.is_long)} vs {r['side']}, "
                         f"stop differs by {rel:.1e} of price")
    return diffs


ROLES: dict[str, Callable[[RoleContext], Awaitable[RoleResult]]] = {
    "collector": collector,
    "analyst": analyst,
    "risk": risk,
    "executor": executor,
    "controller": controller,
}

#: Shift order. By day there are no signals or approvals — and no entries.
SHIFTS: dict[str, tuple[str, ...]] = {
    "night": ("collector", "analyst", "risk", "executor", "controller"),
    "day": ("collector", "executor", "controller"),
}


async def run_role(name: str, ctx: RoleContext) -> RoleResult:
    """Runs a role and records it on the board: start, result, candidate paths."""
    started = time.monotonic()
    run_id = await ctx.board.start_run(name, ctx.board_book, ctx.moment, ctx.shift, {
        "now": ctx.now.isoformat(), "execute": ctx.execute, "venue": ctx.venue})
    try:
        result = await ROLES[name](ctx)
    except Exception as exc:  # noqa: BLE001 — a role failure is a result too, on the board
        result = RoleResult(status="failed", summary=f"{type(exc).__name__}: {exc}",
                            exit_code=EXIT_FAILED, error=repr(exc))
    result.metrics["seconds_total"] = round(time.monotonic() - started, 2)
    await ctx.board.add_traces(ctx.board_book, ctx.moment, run_id, result.traces)
    if result.intents is not None:
        await ctx.board.save_intents(ctx.board_book, ctx.moment, run_id, result.intents)
    await ctx.board.finish_run(run_id, status=result.status, summary=result.summary,
                               outputs=result.outputs, metrics=result.metrics,
                               error=result.error)
    return result


def shift_code(shift: str, results: dict[str, RoleResult]) -> int:
    """Final shift code for the schedule — the Executor's code.

    At night an entry without approval is impossible, and if the Risk Manager
    gave no legitimate answer (failed, did not match the Analyst), the day is
    not counted: code 1 — the schedule retries in an hour. The Executor has
    already done the exits, so a retry is safe. The Controller does not change
    the code while in shadow.
    """
    executor_result = results.get("executor")
    if executor_result is None:
        return EXIT_FAILED
    code = executor_result.exit_code
    if shift == "night" and code in (EXIT_OK, EXIT_PARTIAL):
        signals, approvals = results.get("analyst"), results.get("risk")
        analyst_failed = signals is None or signals.status == "failed"
        risk_failed = approvals is None or approvals.status == "failed" or (
            approvals.status == "refused" and signals is not None and signals.status == "ok")
        if analyst_failed or risk_failed:
            return EXIT_FAILED
    return code


__all__ = [
    "EXIT_FAILED",
    "EXIT_HALTED",
    "EXIT_OK",
    "EXIT_PARTIAL",
    "ROLES",
    "SHADOW_NIGHTS",
    "SHIFTS",
    "RoleContext",
    "RoleResult",
    "candidate_rows",
    "exit_code_of",
    "make_gate",
    "run_role",
    "shift_code",
]
