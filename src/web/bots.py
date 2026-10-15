"""Bot windows and the trade path on the panel (DEC-049). Read-only on the board.

* `/api/bots` — per bot: the latest run in full (what it received,
  what it did, what it handed on) and the run history for charts;
* `/api/paths` — the path of each symbol of the day through the bots: signal → risk →
  execution → check, the outcome and the rejection reason. Rule rejections and
  platform rejections are counted separately: a rejection caused by the Mac sleeping is
  not a property of the strategy and is not mixed into the 60-trade sample.

A night retry (code 1 → another attempt an hour later) writes the path afresh. The latest
attempt is shown: the Analyst's traces from its latest night run, the others from runs
after it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select

from src.core.db import BotRun, DecisionTrace, session_scope

router = APIRouter()

BOTS = ("collector", "analyst", "risk", "executor", "controller")
STAGES = ("signal", "risk", "execution", "verify")
#: Night — the first run after 00:00 UTC, at :05; day — every hour at :05.
RUN_MINUTE = timedelta(minutes=5)


def _ts(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


def _run(r: BotRun, *, full: bool) -> dict:
    seconds = (r.finished_at - r.started_at).total_seconds() if r.finished_at else None
    row = {
        "id": r.id, "bot": r.bot, "shift": r.shift, "moment": _ts(r.moment),
        "started_at": _ts(r.started_at), "finished_at": _ts(r.finished_at),
        "seconds": None if seconds is None else round(seconds, 2),
        "status": r.status, "summary": r.summary, "metrics": r.metrics or {},
    }
    if full:
        row |= {"inputs": r.inputs or {}, "outputs": r.outputs or {}, "error": r.error}
    return row


def next_runs(now: datetime) -> dict[str, str]:
    night = now.replace(hour=0, minute=0, second=0, microsecond=0) + RUN_MINUTE
    if night <= now:
        night += timedelta(days=1)
    hour = now.replace(minute=0, second=0, microsecond=0) + RUN_MINUTE
    if hour <= now:
        hour += timedelta(hours=1)
    return {"night": _ts(night), "day": _ts(hour)}


@router.get("/api/bots")
async def bots(book: str = "setup_e:paper", limit: int = Query(40, ge=1, le=400)) -> dict:
    out = []
    async with session_scope() as s:
        for bot in BOTS:
            rows = (await s.execute(
                select(BotRun).where(BotRun.book == book, BotRun.bot == bot)
                .order_by(BotRun.id.desc()).limit(limit)
            )).scalars().all()
            night = next((r for r in rows if r.shift == "night"), None)
            day = next((r for r in rows if r.shift == "day"), None)
            out.append({
                "bot": bot,
                "latest": _run(rows[0], full=True) if rows else None,
                "latest_night": _run(night, full=True) if night else None,
                "latest_day": _run(day, full=True) if day else None,
                "runs": [_run(r, full=False) for r in rows],
            })
        last_night = (await s.execute(
            select(func.max(BotRun.moment)).where(BotRun.book == book, BotRun.shift == "night")
        )).scalar()
    return {"now": _ts(datetime.now(UTC)), "next": next_runs(datetime.now(UTC)),
            "last_night": _ts(last_night), "bots": out}


# ── trade path ──────────────────────────────────────────────
@dataclass(frozen=True)
class Step:
    id: int
    run_id: int | None
    symbol: str
    stage: str
    verdict: str
    code: str | None
    origin: str
    detail: dict
    ts: datetime | None = None


def assemble(steps: list[Step], anchor: int | None) -> dict:
    """The path of each symbol of one night, and a summary.

    `anchor` — id of the Analyst's latest night run: traces before it are
    an earlier attempt on the same night. Closes (verdict=info) come from any
    runs of the moment, including day runs.
    """
    current = [s for s in steps if anchor is None or (s.run_id or 0) >= anchor]
    latest: dict[tuple[str, str], Step] = {}
    for s in sorted(current, key=lambda s: s.id):
        if s.verdict != "info":
            latest[(s.symbol, s.stage)] = s
    symbols = sorted({sym for sym, _ in latest})
    rows = []
    by_code: Counter[tuple[str, str, str]] = Counter()
    outcome_count: Counter[str] = Counter()
    for sym in symbols:
        stages = {st: latest.get((sym, st)) for st in STAGES}
        outcome = _outcome(stages)
        outcome_count[outcome["kind"]] += 1
        if outcome["kind"] in ("rule", "platform"):
            by_code[(outcome["code"] or "?", outcome["kind"], outcome["stage"])] += 1
        rows.append({
            "symbol": sym,
            "stages": {st: None if s is None else {"verdict": s.verdict, "code": s.code,
                                                    "origin": s.origin, "detail": s.detail}
                       for st, s in stages.items()},
            "outcome": outcome,
        })
    passed = Counter(st for (_, st), s in latest.items() if s.verdict == "pass")
    closes = [{"symbol": s.symbol, "reason": s.code, "pnl": s.detail.get("pnl"),
               "ts": _ts(s.ts)}
              for s in sorted(steps, key=lambda s: s.id) if s.verdict == "info"]
    order = {"entered": 0, "flagged": 1, "pending": 2, "platform": 3, "rule": 4}
    rows.sort(key=lambda r: (order[r["outcome"]["kind"]], r["symbol"]))
    return {
        "rows": rows,
        "funnel": {st: passed.get(st, 0) for st in STAGES},
        "outcomes": {k: outcome_count.get(k, 0)
                     for k in ("entered", "flagged", "rule", "platform", "pending")},
        "by_code": [{"code": c, "origin": o, "stage": st, "count": n}
                    for (c, o, st), n in by_code.most_common()],
        "closes": closes,
    }


def _outcome(stages: dict[str, Step | None]) -> dict:
    execution, verify = stages["execution"], stages["verify"]
    if execution is not None and execution.verdict == "pass":
        if verify is not None and verify.verdict == "fail":
            return {"kind": "flagged", "stage": "verify", "code": verify.code}
        return {"kind": "entered", "stage": "execution", "code": None}
    for st in STAGES:
        s = stages[st]
        if s is not None and s.verdict in ("reject", "fail"):
            return {"kind": s.origin, "stage": st, "code": s.code}
    last = next((st for st in reversed(STAGES) if stages[st] is not None), "signal")
    return {"kind": "pending", "stage": last, "code": None}


async def _steps(book: str, moments: list[datetime]) -> dict[datetime, list[Step]]:
    if not moments:
        return {}
    async with session_scope() as s:
        rows = (await s.execute(
            select(DecisionTrace).where(DecisionTrace.book == book,
                                        DecisionTrace.moment.in_(moments))
        )).scalars().all()
    out: dict[datetime, list[Step]] = {m: [] for m in moments}
    for r in rows:
        out.setdefault(r.moment, []).append(Step(
            r.id, r.bot_run_id, r.symbol, r.stage, r.verdict, r.code, r.origin,
            r.detail or {}, r.ts))
    return out


async def _anchors(book: str, moments: list[datetime]) -> dict[datetime, int]:
    if not moments:
        return {}
    async with session_scope() as s:
        rows = (await s.execute(
            select(BotRun.moment, func.max(BotRun.id))
            .where(BotRun.book == book, BotRun.bot == "analyst", BotRun.shift == "night",
                   BotRun.moment.in_(moments))
            .group_by(BotRun.moment)
        )).all()
    return dict(rows)


@router.get("/api/paths")
async def paths(book: str = "setup_e:paper", moment: str | None = None,
                nights: int = Query(30, ge=1, le=180)) -> dict:
    try:
        asked = datetime.fromisoformat(moment) if moment else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"moment is not ISO: {moment!r}") from exc
    async with session_scope() as s:
        known = (await s.execute(
            select(DecisionTrace.moment).where(DecisionTrace.book == book)
            .group_by(DecisionTrace.moment).order_by(DecisionTrace.moment.desc()).limit(nights)
        )).scalars().all()
    chosen = asked or (known[0] if known else None)
    wanted = sorted(set(known) | ({chosen} if chosen else set()))
    steps, anchors = await _steps(book, wanted), await _anchors(book, wanted)
    trend = []
    for m in sorted(known):
        day = assemble(steps.get(m, []), anchors.get(m))
        trend.append({"moment": _ts(m), **day["outcomes"],
                      "signals": day["funnel"]["signal"]})
    day = assemble(steps.get(chosen, []), anchors.get(chosen)) if chosen else None
    return {"moments": [_ts(m) for m in known], "moment": _ts(chosen), "day": day,
            "trend": trend}


__all__ = ["Step", "assemble", "next_runs", "router"]
