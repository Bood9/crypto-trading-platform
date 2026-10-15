"""Bot platform (DEC-049): roles, board, two keys, shift code.

Roles run end to end on an in-memory board and an in-memory book; the paper
exchange is a file in a temp folder, as in production: the Executor writes it,
the Risk Manager's rehearsal does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

import src.live.runner as runner
from src.bots.board import MemoryBoard, Trace, origin_of
from src.bots.roles import (
    EXIT_FAILED,
    EXIT_HALTED,
    EXIT_OK,
    EXIT_PARTIAL,
    RoleContext,
    RoleResult,
    candidate_rows,
    make_gate,
    run_role,
    shift_code,
)
from src.core.config import load_settings
from src.execution.plan import InstrumentSpec, Order, OrderKind
from src.live.cycle import PlannedEntry
from src.live.paper_venue import MarketPaperVenue
from src.live.runner import Prepared
from src.live.signals import Candidate, Skip
from src.live.store import LiveTrade, MemoryStore

D = Decimal
DAY = timedelta(days=1)
M = datetime(2026, 10, 5, tzinfo=UTC)
NOW = M + timedelta(minutes=5)
BOOK = "setup_e:paper"
SPEC = {s: InstrumentSpec(s, D("0.01"), D("0.001"), D("0.001"), D("1e6"), D(5))
        for s in ("AUSDT", "BUSDT", "CUSDT")}


def cand(symbol: str = "BUSDT", close: str = "50", stop: str = "45") -> Candidate:
    return Candidate(symbol, M - DAY, True, D(close), D(stop))


def held_trade(due: datetime) -> LiveTrade:
    return LiveTrade(
        setup=BOOK, symbol="AUSDT", is_long=True, qty=D("0.1"), entry_price=D(100),
        stop_price=D(40), margin=D(2), entry_link_id="e-A", stop_link_id="s-A",
        bar_ts=M - DAY * 8, opened_at=due - DAY * 7, close_due_at=due,
        status="open", entry_fee=D("0.005"),
    )


class Harness:
    """One night's environment: universe, signals, the paper exchange file."""

    def __init__(self, monkeypatch, tmp_path, prepared: Prepared, marks: dict[str, str]):
        self.prepared = prepared
        self.marks = {s: D(v) for s, v in marks.items()}
        self.path = tmp_path / "setup_e.json"
        self.store = MemoryStore()
        self.board = MemoryBoard()
        monkeypatch.setattr(runner, "PAPER_DIR", tmp_path)
        h = self

        async def fake_prepare(_s, _setup, _now):
            # Each role gets its own copy — as if from the database.
            p = h.prepared
            return Prepared(p.moment, list(p.universe), list(p.candidates), dict(p.skips),
                            problem=p.problem)

        async def fake_paper_venue(_s, _setup, _symbols, as_of):
            venue = MarketPaperVenue.load(h.path)
            venue.mark, venue.specs = dict(h.marks), dict(SPEC)
            complete_before = as_of.replace(minute=0, second=0, microsecond=0)
            replayed = venue.replayed_until or complete_before
            venue.seen_until = max(replayed, venue.seen_until or replayed)
            venue.decision_passed = venue.seen_until > complete_before
            venue.replayed_until = max(replayed, complete_before)
            return venue, h.path, []

        async def nothing(*_a):
            return {}

        async def zero(*_a):
            return D(0)

        class Uncorrelated:
            def __init__(self, *_a):
                pass

            async def between(self, *_a):
                return D("0.1")

        monkeypatch.setattr(runner, "prepare", fake_prepare)
        monkeypatch.setattr(runner, "paper_venue", fake_paper_venue)
        monkeypatch.setattr(runner, "load_instrument_params", nothing)
        monkeypatch.setattr(runner, "funding_sum", zero)
        monkeypatch.setattr(runner, "CorrelationSource", Uncorrelated)

    async def hold_a(self, due: datetime) -> None:
        venue = MarketPaperVenue(mark={"AUSDT": D(100)}, specs=dict(SPEC))
        await venue.place(Order(OrderKind.ENTRY, "AUSDT", True, D("0.1"), None, False, "e-A"))
        await venue.place(Order(OrderKind.STOP_LOSS, "AUSDT", False, D("0.1"), D(40), True,
                                "s-A"))
        venue.replayed_until = M
        venue.save(self.path)
        await self.store.add_trade(held_trade(due))

    def ctx(self, shift: str = "night", now: datetime = NOW) -> RoleContext:
        return RoleContext(settings=load_settings(), board=self.board, setup="setup_e",
                           venue="paper", shift=shift, now=now, store=self.store)

    async def night(self, roles=("analyst", "risk", "executor", "controller"), now=NOW):
        return {name: await run_role(name, self.ctx(now=now)) for name in roles}

    def traces(self, symbol: str) -> list[tuple[str, str, str | None]]:
        return [(t.stage, t.verdict, t.code) for *_, t in self.board.traces
                if t.symbol == symbol]


# ── whole night ─────────────────────────────────────────────
async def test_night_enters_only_with_both_keys_and_traces_every_stage(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    results = await h.night()
    assert {n: r.status for n, r in results.items()} == {
        "analyst": "ok", "risk": "ok", "executor": "ok", "controller": "ok"}
    assert results["executor"].outputs["opened"] == ["BUSDT"]
    intent = (await h.board.intents(BOOK, M))["BUSDT"]
    trade = next(t for t in h.store.trades if t.symbol == "BUSDT")
    assert (intent.qty, intent.stop_price) == (trade.qty, trade.stop_price)
    assert h.traces("BUSDT") == [("signal", "pass", None), ("risk", "pass", None),
                                 ("execution", "pass", None), ("verify", "pass", None)]
    assert shift_code("night", results) == EXIT_OK
    checks = {c["check"]: c["ok"] for c in results["controller"].outputs["checks"]}
    assert checks == {"accounting": True, "paper marks": True, "protection BUSDT": True,
                      "two keys": True}


async def test_rehearsal_writes_nothing(monkeypatch, tmp_path):
    """Risk Manager rehearsal: no trade, no journal, no state,
    the exchange file is byte-for-byte unchanged."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]),
                {"AUSDT": "50", "BUSDT": "50"})
    await h.hold_a(due=M)
    before = h.path.read_bytes()
    trades_before = [t.status for t in h.store.trades]
    results = await h.night(("analyst", "risk"))
    assert results["risk"].outputs["approved"] == ["BUSDT"]
    assert results["risk"].outputs["would_close"][0][:2] == ["AUSDT", "TIME_STOP"]
    assert h.path.read_bytes() == before
    assert [t.status for t in h.store.trades] == trades_before
    assert h.store.entries == [] and h.store.states == {}


async def test_rehearsal_sizes_after_todays_exits_like_the_executor(monkeypatch, tmp_path):
    """A day of time exits: an AAVE-like position closes at a loss
    of $5, equity 95. The Risk Manager sizes the entry from 95, like the
    Executor; a preview without execution would size from 100 and the second key
    would reject (RISK_MISMATCH)."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]),
                {"AUSDT": "50", "BUSDT": "50"})
    await h.hold_a(due=M)
    _, preview, _ = await runner.run_daily(load_settings(), "setup_e", venue_kind="paper",
                                           execute=False, now=NOW, store=h.store)
    results = await h.night()
    intent = (await h.board.intents(BOOK, M))["BUSDT"]
    assert preview.planned[0].qty != intent.qty, "check is meaningless: size does not depend"
    assert results["executor"].outputs["opened"] == ["BUSDT"], results["executor"].outputs
    assert [c[:2] for c in results["executor"].outputs["closed"]] == [["AUSDT", "TIME_STOP"]]
    assert shift_code("night", results) == EXIT_OK
    checks = {c["check"]: c["ok"] for c in results["controller"].outputs["checks"]}
    assert checks["accounting"] is True


async def test_executor_without_approvals_exits_but_does_not_enter(monkeypatch, tmp_path):
    """The Risk Manager did not run: time exit — yes, entry — no, and the night
    is not counted (retry in an hour)."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]),
                {"AUSDT": "50", "BUSDT": "50"})
    await h.hold_a(due=M)
    results = await h.night(("analyst", "executor"))
    out = results["executor"].outputs
    assert out["opened"] == [] and out["skipped"] == {"BUSDT": "RISK_NOT_APPROVED"}
    assert [c[:2] for c in out["closed"]] == [["AUSDT", "TIME_STOP"]]
    assert ("execution", "reject", "RISK_NOT_APPROVED") in h.traces("BUSDT")
    assert (results["executor"].status, results["executor"].exit_code) == ("refused",
                                                                           EXIT_FAILED)
    assert MarketPaperVenue.load(h.path).decided_for is None, "day's decision not counted"
    assert shift_code("night", results) == EXIT_FAILED


async def test_late_attempt_without_approvals_leaves_the_retry_able_to_enter(
        monkeypatch, tmp_path):
    """Integration review: first attempt at 02:10 UTC, the Risk Manager crashed.
    The Executor must not count the decision and run the exchange up to 02:00 —
    otherwise the retry at 03:10 would see the exchange after the decision and not enter."""
    from src.backtest.engine import MarketBar

    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]),
                {"AUSDT": "100", "BUSDT": "50"})
    await h.hold_a(due=M + DAY * 3)
    hour = timedelta(hours=1)
    bars = [MarketBar(M + hour * i, D(100), D(101), D(99), D(100), D(1)) for i in range(4)]

    async def fake_bars(symbol, _tf, start, end):
        return [b for b in bars if symbol == "AUSDT" and start <= b.ts < end]

    monkeypatch.setattr(runner, "load_bars", fake_bars)
    first = await h.night(("analyst", "executor"), now=M + hour * 2 + timedelta(minutes=10))
    assert first["executor"].status == "refused"
    venue = MarketPaperVenue.load(h.path)
    assert (venue.replayed_until, venue.seen_until, venue.decided_for) == (M, M, None)

    retry = await h.night(now=M + hour * 3 + timedelta(minutes=10))
    assert retry["executor"].outputs["opened"] == ["BUSDT"], retry["executor"].outputs
    assert shift_code("night", retry) == EXIT_OK
    assert MarketPaperVenue.load(h.path).decided_for == M


async def test_executor_ignores_approvals_left_by_an_earlier_attempt(monkeypatch, tmp_path):
    """Retry: the Risk Manager crashed, the board holds approvals from the previous attempt.
    Only the latest run with status ok can be trusted."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    await h.night(("analyst", "risk"))
    assert (await h.board.intents(BOOK, M))["BUSDT"].verdict == "approve"
    run_id = await h.board.start_run("risk", BOOK, M, "night", {})
    await h.board.finish_run(run_id, status="failed", summary="crashed", outputs={}, metrics={},
                             error="boom")
    result = await run_role("executor", h.ctx())
    assert result.outputs["opened"] == [] and result.outputs["approvals_valid"] is False


async def test_signal_mismatch_leaves_no_approvals(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    await run_role("analyst", h.ctx())
    h.prepared = Prepared(M, ["BUSDT"], [cand(stop="44")])  # the Risk Manager sees something else
    results = {"analyst": RoleResult("ok", "")}
    results["risk"] = await run_role("risk", h.ctx())
    results["executor"] = await run_role("executor", h.ctx())
    assert results["risk"].status == "refused"
    assert await h.board.intents(BOOK, M) == {}
    assert results["executor"].outputs["opened"] == []
    assert ("risk", "reject", "SIGNAL_MISMATCH") in h.traces("BUSDT")
    assert shift_code("night", results) == EXIT_FAILED


async def test_rule_rejections_are_recorded_with_their_code(monkeypatch, tmp_path):
    """A position in the symbol is already open — a rule rejection, not a platform one."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["AUSDT"], [cand("AUSDT", "100", "90")]),
                {"AUSDT": "100"})
    await h.hold_a(due=M + DAY * 3)
    results = await h.night()
    assert results["risk"].outputs["rejected"] == {"AUSDT": "MAX_POSITIONS"}
    rejects = [t for *_, t in h.board.traces if t.stage == "risk"]
    assert [(t.code, t.origin) for t in rejects] == [("MAX_POSITIONS", "rule")]
    assert shift_code("night", results) == EXIT_OK


async def test_retry_after_decision_is_rejected_as_decision_done(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    venue = MarketPaperVenue()
    venue.replayed_until = venue.seen_until = M + timedelta(hours=3)
    venue.save(h.path)
    results = await h.night()
    assert results["risk"].outputs["rejected"] == {"BUSDT": "DECISION_DONE"}
    assert origin_of("DECISION_DONE") == "platform"
    assert results["executor"].outputs["opened"] == []


async def test_analyst_skips_are_counted_and_traced_without_no_signal(monkeypatch, tmp_path):
    skips = {"XUSDT": Skip.NO_SIGNAL, "YUSDT": Skip.STALE_DATA, "ZUSDT": Skip.SHORT_HISTORY}
    h = Harness(monkeypatch, tmp_path,
                Prepared(M, ["BUSDT", "XUSDT", "YUSDT", "ZUSDT"], [cand()], skips),
                {"BUSDT": "50"})
    result = await run_role("analyst", h.ctx())
    assert result.metrics | {"seconds_total": 0} == {
        "universe": 4, "candidates": 1, "no_signal": 1, "short_history": 1, "stale": 1,
        "seconds_total": 0}
    assert result.outputs["stale"] == ["YUSDT"]
    traced = {t.symbol: (t.code, t.origin) for *_, t in h.board.traces}
    assert traced == {"BUSDT": (None, "rule"), "YUSDT": ("STALE_DATA", "platform"),
                      "ZUSDT": ("SHORT_HISTORY", "rule")}


async def test_no_universe_refuses_downstream_and_fails_the_night(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, Prepared(M, [], [], problem="no universe"), {})
    results = await h.night()
    assert results["analyst"].status == "refused"
    assert results["risk"].status == "refused"
    assert results["executor"].exit_code == EXIT_FAILED
    assert shift_code("night", results) == EXIT_FAILED


# ── second key ─────────────────────────────────────────────
@pytest.mark.parametrize(("intent_qty", "is_long", "expected"), [
    (D("0.100"), True, None),
    (D("0.1000000000000000001"), True, None),  # base rounding is not a mismatch
    (D("0.101"), True, "RISK_MISMATCH"),
    (D("0.100"), False, "RISK_MISMATCH"),
])
async def test_gate_on_paper_requires_the_same_plan(intent_qty, is_long, expected):
    from src.bots.board import Intent

    planned = PlannedEntry("BUSDT", True, D("0.1"), D(50), D(45))
    gate = make_gate({"BUSDT": Intent("BUSDT", "approve", None, is_long, intent_qty, D(50),
                                      D(45))}, exact=True)
    assert await gate(planned) == expected


async def test_gate_rejects_missing_or_refused_approval():
    from src.bots.board import Intent

    planned = PlannedEntry("BUSDT", True, D("0.1"), D(50), D(45))
    assert await make_gate({}, exact=True)(planned) == "RISK_NOT_APPROVED"
    refused = {"BUSDT": Intent("BUSDT", "reject", "MAX_MARGIN")}
    assert await make_gate(refused, exact=False)(planned) == "RISK_NOT_APPROVED"
    other_size = {"BUSDT": Intent("BUSDT", "approve", None, True, D(7), D(51), D(40))}
    assert await make_gate(other_size, exact=False)(planned) is None  # testnet: side


# ── Controller ──────────────────────────────────────────────
async def test_controller_flags_an_entry_without_approval(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    run_id = await h.board.start_run("executor", BOOK, M, "night", {})
    await h.board.finish_run(run_id, status="ok", summary="", outputs={"opened": ["BUSDT"]},
                             metrics={}, error=None)
    result = await run_role("controller", h.ctx())
    assert result.status == "failed" and "two keys" in result.summary
    assert ("verify", "fail", "RISK_NOT_APPROVED") in h.traces("BUSDT")
    # In shadow mode: the shift code is the Executor's code.
    code = shift_code("night", {"analyst": RoleResult("ok", ""), "risk": RoleResult("ok", ""),
                                "executor": RoleResult("ok", ""), "controller": result})
    assert code == EXIT_OK


async def test_controller_catches_equity_that_does_not_add_up(monkeypatch, tmp_path):
    from src.risk.governor import PortfolioState

    h = Harness(monkeypatch, tmp_path, Prepared(M, [], []), {})
    h.store.states[BOOK] = PortfolioState.start(D("97"), M)
    result = await run_role("controller", h.ctx("day"))
    assert result.outputs["checks"][0] == {"check": "accounting", "ok": False,
                                           "detail": "equity 97.000000 $, from closed trades "
                                                     "100.000000 $"}


async def test_controller_accepts_a_stop_executed_after_the_cycle(monkeypatch, tmp_path):
    """A late run moved the exchange after the cycle and the stop fired: the trade
    is still open in the accounting — this is not a "position without protection"."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, [], []), {})
    await h.hold_a(due=M + DAY * 3)
    venue = MarketPaperVenue.load(h.path)
    venue.specs = dict(SPEC)
    venue.move_price("AUSDT", D(30))  # stop 40 executed
    venue.save(h.path)
    result = await run_role("controller", h.ctx("day"))
    guard = next(c for c in result.outputs["checks"] if c["check"] == "protection AUSDT")
    assert guard["ok"] and "will be booked by the next run" in guard["detail"]


async def test_controller_flags_a_position_without_its_stop(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, Prepared(M, [], []), {})
    await h.hold_a(due=M + DAY * 3)
    venue = MarketPaperVenue.load(h.path)
    await venue.cancel("s-A")
    venue.save(h.path)
    result = await run_role("controller", h.ctx("day"))
    assert result.status == "failed" and "protection AUSDT" in result.summary


async def test_halted_late_run_does_not_move_the_venue(monkeypatch, tmp_path):
    """Core review: a late cycle halted by reconciliation does not move the exchange
    forward — after trade-resume the retry decides "for 00:10"."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    venue = MarketPaperVenue()
    venue.replayed_until = M
    venue.save(h.path)
    await h.store.add_trade(held_trade(M + DAY * 3))  # in the accounting, not on the exchange
    _, report, _ = await runner.run_daily(load_settings(), "setup_e", venue_kind="paper",
                                          execute=True, store=h.store,
                                          now=M + timedelta(hours=2, minutes=10))
    assert report.halted
    after = MarketPaperVenue.load(h.path)
    assert (after.replayed_until, after.decided_for) == (M, None)


async def test_dry_shift_writes_under_its_own_book_and_only_previews(monkeypatch, tmp_path):
    """A preview does not replace the day's real approvals and does not try to execute."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]), {"BUSDT": "50"})
    await h.night(("analyst", "risk"))
    real = await h.board.intents(BOOK, M)
    ctx = h.ctx()
    ctx.execute = False
    for name in ("analyst", "risk", "executor"):
        result = await run_role(name, ctx)
    assert await h.board.intents(BOOK, M) == real
    assert (await h.board.intents(BOOK + ":dry", M))["BUSDT"].verdict == "approve"
    dry_risk = await h.board.latest_run("risk", BOOK + ":dry", M)
    assert dry_risk.outputs["rehearsal"] is False
    assert result.outputs["opened"] == ["BUSDT"]  # preview: "would open"
    assert h.store.trades == []


async def test_close_is_in_equity_even_if_the_cycle_fails_later(monkeypatch, tmp_path):
    """Core review: the trade is closed and recorded, then a failure (database, disk) —
    the result is already in equity, not lost."""
    h = Harness(monkeypatch, tmp_path, Prepared(M, ["BUSDT"], [cand()]),
                {"AUSDT": "50", "BUSDT": "50"})
    await h.hold_a(due=M)

    class Broken(Exception):
        pass

    async def boom(*_a):
        raise Broken("database is gone")

    monkeypatch.setattr(h.store, "add_trade", boom)  # fails on the BUSDT entry
    with pytest.raises(Broken):
        await runner.run_daily(load_settings(), "setup_e", venue_kind="paper", execute=True,
                               now=NOW, store=h.store)
    closed = next(t for t in h.store.trades if t.symbol == "AUSDT")
    assert closed.status == "closed"
    assert h.store.states[BOOK].equity == D(100) + closed.pnl


# ── Collector ───────────────────────────────────────────────
async def test_collector_builds_the_month_when_due(monkeypatch, tmp_path):
    calls: list[str] = []
    codes = {"--check": 5}

    async def fake_cli(args, _log):
        calls.append(" ".join(args[:3]))
        return next((c for flag, c in codes.items() if flag in args), 0)

    h = Harness(monkeypatch, tmp_path, Prepared(M, [], []), {})
    ctx = h.ctx()
    ctx.run_cli = fake_cli
    result = await run_role("collector", ctx)
    assert calls == ["backfill --symbols universe-now", "build-universe --month due",
                     "backfill --symbols candidates", "build-universe --month due",
                     "backfill --symbols universe-now"]
    assert result.status == "ok" and result.metrics["steps"] == 5


async def test_collector_does_not_build_from_a_failed_candidate_pull(monkeypatch, tmp_path):
    calls: list[str] = []

    async def fake_cli(args, _log):
        calls.append(args[2])
        return {"candidates": 1}.get(args[2], 5 if "--check" in args else 0)

    h = Harness(monkeypatch, tmp_path, Prepared(M, [], []), {})
    ctx = h.ctx()
    ctx.run_cli = fake_cli
    result = await run_role("collector", ctx)
    assert calls == ["universe-now", "due", "candidates"]
    assert result.status == "failed" and "4h for all candidates" in result.summary


async def test_day_collector_pulls_only_held_hourly_bars(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    async def fake_cli(args, _log):
        seen.append(args)
        return 0

    h = Harness(monkeypatch, tmp_path, Prepared(M, [], []), {})
    ctx = h.ctx("day")
    ctx.run_cli = fake_cli
    await run_role("collector", ctx)
    assert seen == [["backfill", "--symbols", "held", "--streams", "kline_1h,funding",
                     "--years", "0.05"]]


# ── shift code and board ────────────────────────────────────
@pytest.mark.parametrize(("executor", "expected"), [
    (EXIT_OK, EXIT_OK), (EXIT_PARTIAL, EXIT_PARTIAL), (EXIT_HALTED, EXIT_HALTED),
    (EXIT_FAILED, EXIT_FAILED),
])
def test_shift_code_is_the_executors(executor, expected):
    ok = RoleResult("ok", "")
    results = {"analyst": ok, "risk": ok, "executor": RoleResult("ok", "", exit_code=executor),
               "controller": RoleResult("failed", "")}
    assert shift_code("night", results) == expected


def test_day_shift_ignores_missing_night_roles():
    assert shift_code("day", {"executor": RoleResult("skipped", "")}) == EXIT_OK
    assert shift_code("day", {}) == EXIT_FAILED


def test_crashed_role_is_a_failed_run_on_the_board(monkeypatch):
    import src.bots.roles as roles

    async def boom(_ctx):
        raise RuntimeError("no connection")

    monkeypatch.setitem(roles.ROLES, "analyst", boom)
    board = MemoryBoard()
    ctx = RoleContext(settings=load_settings(), board=board, setup="setup_e", venue="paper",
                      shift="night", now=NOW, store=MemoryStore())
    import asyncio

    result = asyncio.run(run_role("analyst", ctx))
    assert (result.status, result.exit_code) == ("failed", EXIT_FAILED)
    assert board.runs[0]["status"] == "failed" and "no connection" in board.runs[0]["summary"]


def test_platform_and_rule_codes_are_split():
    assert origin_of("RISK_NOT_APPROVED") == "platform"
    assert origin_of("STALE_DATA") == "platform"
    assert origin_of("MAX_POSITIONS") == "rule"
    assert Trace("X", "risk", "pass").origin == "rule"
    assert Trace("X", "execution", "reject", "ENTRY_WINDOW_MISSED").origin == "platform"


def test_candidate_rows_are_the_contract_between_analyst_and_risk():
    rows = candidate_rows([cand(), cand("CUSDT", "2.50", "2.3")])
    assert rows[1] == {"symbol": "CUSDT", "side": "long", "close": "2.5", "stop": "2.3",
                       "bar": (M - DAY).isoformat(), "queue": 2}


def test_conductor_requires_one_now_for_a_role():
    from src.bots.__main__ import main

    with pytest.raises(SystemExit):
        main(["role", "risk", "--shift", "night"])


def test_conductor_runs_roles_in_order_and_returns_the_shift_code(monkeypatch, tmp_path):
    import src.bots.__main__ as conductor

    order: list[tuple[str, str]] = []

    def fake_role(name, args, now):
        order.append((name, now.isoformat()))
        return RoleResult("failed" if name == "controller" else "ok", "check",
                          exit_code=EXIT_PARTIAL if name == "executor" else EXIT_OK)

    monkeypatch.setattr(conductor, "_role_process", fake_role)
    monkeypatch.setattr(conductor, "LOGS", tmp_path)
    code = conductor.main(["night", "--now", NOW.isoformat()])
    assert [n for n, _ in order] == ["collector", "analyst", "risk", "executor", "controller"]
    assert {t for _, t in order} == {NOW.isoformat()}
    assert code == EXIT_PARTIAL
    # A run for a given moment is manual: it does not use up the day's alert.
    assert not list(tmp_path.glob("controller_failed_*"))
    conductor.main(["night"])
    assert len(list(tmp_path.glob("controller_failed_*"))) == 1


def test_killing_a_role_takes_its_children_with_it():
    """A Collector killed on timeout does not leave data collection running."""
    import os
    import subprocess

    from src.bots.__main__ import _kill

    proc = subprocess.Popen(["sh", "-c", "sleep 30 & sleep 30"], process_group=0)
    _kill(proc)
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)


async def test_abandoned_run_is_marked_failed():
    board = MemoryBoard()
    run_id = await board.start_run("collector", BOOK, M, "night", {})
    assert await board.abandon("collector", BOOK, M, "night", "killed on timeout") == 1
    assert board.runs[run_id - 1]["status"] == "failed"
    assert await board.abandon("collector", BOOK, M, "night", "again") == 0


@pytest.mark.parametrize(("shift", "alert"), [("night", True), ("day", False)])
def test_failed_night_collector_raises_an_alert(monkeypatch, tmp_path, shift, alert):
    """Night audit 2026-09-30: a month universe build that failed at night must
    not stay silent until the next night. In the daytime a collection failure is ordinary
    network noise; there is no need to alert about it every hour."""
    import src.bots.__main__ as conductor

    def fake_role(name, _args, _now):
        if name == "collector":
            return RoleResult("failed", "failure: building the month's universe",
                              exit_code=EXIT_FAILED)
        return RoleResult("ok", "")

    monkeypatch.setattr(conductor, "_role_process", fake_role)
    monkeypatch.setattr(conductor, "LOGS", tmp_path)
    assert conductor.main([shift]) == EXIT_OK  # collection does not change the shift code
    files = list(tmp_path.glob("collector_failed_*"))
    assert bool(files) is alert
    if alert:
        assert "building the month's universe" in files[0].read_text()
