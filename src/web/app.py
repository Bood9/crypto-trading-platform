"""Panel for watching the live cycle. Phase 5, DEC-041.

**Read-only.** The panel places no orders and does not turn trading on:
resuming after a halt is a deliberate human action on the command line
(`trade-resume --reason`), not a button that can be pressed by accident.
Listens on localhost only.

All numbers are computed on the server (`metrics.py`); the browser only draws them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, union

from src.core.config import get_settings
from src.core.db import (
    FundingHistory,
    LiveJournal,
    LivePosition,
    LiveState,
    Ohlcv,
    UniverseSnapshot,
    session_scope,
)
from src.live.report import JOURNAL
from src.live.store import DbStore, LiveTrade, row_to_trade
from src.web import metrics
from src.web.bots import router as bots_router

STATIC = Path(__file__).parent / "static"
MSK = timedelta(hours=3)
#: Night-run time (UTC) and entry window — as in scripts/daily.sh.
RUN_AT = timedelta(minutes=5)
ENTRY_WINDOW = timedelta(hours=2)

app = FastAPI(title="CRYPTO — panel", docs_url=None, redoc_url=None)


@app.middleware("http")
async def no_stale_assets(request, call_next):  # noqa: ANN001, ANN201
    """The page and its files are always fresh: after a panel update the
    browser must not show yesterday's script with yesterday's logic."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers["Cache-Control"] = "no-store"
    return response


def _num(value: Decimal | None, places: int = 8) -> float | None:
    return None if value is None else round(float(value), places)


def _ts(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment else None


async def _trades(book: str) -> list[LiveTrade]:
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(LivePosition).where(LivePosition.setup == book).order_by(LivePosition.id)
            )
        ).scalars().all()
        return [row_to_trade(r) for r in rows]


async def _first_seen(book: str) -> datetime | None:
    async with session_scope() as s:
        return (
            await s.execute(select(func.min(LiveJournal.ts)).where(
                LiveJournal.setup == book, LiveJournal.dry_run.is_(False)
            ))
        ).scalar()


async def _prices(symbols: list[str], now: datetime) -> dict[str, tuple[Decimal, datetime]]:
    """Last close of the main market's hourly bar, per symbol."""
    if not symbols:
        return {}
    complete = now.replace(minute=0, second=0, microsecond=0)
    out: dict[str, tuple[Decimal, datetime]] = {}
    async with session_scope() as s:
        for symbol in symbols:
            row = (
                await s.execute(
                    select(Ohlcv.ts, Ohlcv.close)
                    .where(Ohlcv.symbol == symbol, Ohlcv.timeframe == "1h", Ohlcv.ts < complete)
                    .order_by(Ohlcv.ts.desc())
                    .limit(1)
                )
            ).first()
            if row:
                out[symbol] = (row[1], row[0] + timedelta(hours=1))
    return out


def _next_run(now: datetime) -> datetime:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0) + RUN_AT
    return today if now < today else today + timedelta(days=1)


def _trade_row(t: LiveTrade) -> dict:
    return {
        "id": t.id,
        "symbol": t.symbol,
        "side": "long" if t.is_long else "short",
        "qty": _num(t.qty),
        "entry": _num(t.entry_price),
        "stop": _num(t.stop_price),
        "notional": _num(t.entry_price * t.qty, 2),
        "risk": _num(abs(t.entry_price - t.stop_price) * t.qty, 4),
        "opened_at": _ts(t.opened_at),
        "due_at": _ts(t.close_due_at),
        "closed_at": _ts(t.closed_at),
        "status": t.status,
        "reason": t.close_reason,
        "exit": _num(t.exit_price),
        "pnl": _num(t.pnl, 4),
        "r": _num(metrics.r_multiple(t), 3),
    }


@app.get("/api/books")
async def books() -> list[str]:
    async with session_scope() as s:
        names = (
            await s.execute(
                union(
                    select(LiveState.setup),
                    select(LivePosition.setup),
                    select(LiveJournal.setup),
                )
            )
        ).scalars().all()
    default = "setup_e:paper"
    return sorted(set(names) | {default}, key=lambda b: (b != default, b))


@app.get("/api/summary")
async def summary(book: str = "setup_e:paper") -> dict:
    settings = get_settings()
    risk = settings.risk
    initial = Decimal(str(risk.equity_usd))
    now = datetime.now(UTC)

    state = await DbStore().load_state(book)
    trades = await _trades(book)
    start = await _first_seen(book)
    st = metrics.stats(trades, initial, start)
    open_trades = [t for t in trades if t.status in ("open", "pending")]

    async with session_scope() as s:
        last_run = (
            await s.execute(select(func.max(LiveJournal.ts)).where(LiveJournal.setup == book))
        ).scalar()
        data_1h = (
            await s.execute(select(func.max(Ohlcv.ts)).where(Ohlcv.timeframe == "1h"))
        ).scalar()

    equity = state.equity if state else initial
    day_loss = state.day_loss_pct() if state else Decimal(0)
    week_loss = state.week_loss_pct() if state else Decimal(0)
    next_run = _next_run(now)
    return {
        "book": book,
        "now": _ts(now),
        "started": state is not None,
        "mode": state.mode.value if state else "ON",
        "equity": _num(equity, 4),
        "initial": _num(initial, 2),
        "day_loss_pct": _num(day_loss, 3),
        "week_loss_pct": _num(week_loss, 3),
        "limits": {
            "daily_loss_pct": risk.daily_loss_limit_pct,
            "weekly_loss_pct": risk.weekly_loss_limit_pct,
            "max_positions": risk.max_concurrent_positions,
            "risk_per_trade_pct": risk.max_loss_per_trade_pct,
            "max_position_pct": risk.max_position_pct,
            "max_total_margin_pct": risk.max_total_margin_pct,
            "margin_per_trade_pct": risk.margin_per_trade_pct,
        },
        "open_positions": len(open_trades),
        "margin_used": _num(sum((t.margin for t in open_trades), Decimal(0)), 4),
        "risk_open": _num(
            sum((abs(t.entry_price - t.stop_price) * t.qty for t in open_trades), Decimal(0)), 4
        ),
        "stats": {
            "closed": st.closed,
            "wins": st.wins,
            "win_rate": _num(st.win_rate, 1),
            "total_pnl": _num(st.total_pnl, 4),
            "avg_r": _num(st.avg_r, 3),
            "best": _num(st.best, 4),
            "worst": _num(st.worst, 4),
            "max_drawdown_pct": _num(st.max_drawdown_pct, 2),
            "forward_target": st.forward_target,
        },
        "last_run": _ts(last_run),
        "next_run": _ts(next_run),
        "entry_window_end": _ts(next_run - RUN_AT + ENTRY_WINDOW),
        "data_1h_until": _ts(data_1h + timedelta(hours=1)) if data_1h else None,
        "msk_offset_hours": 3,
    }


@app.get("/api/equity")
async def equity(book: str = "setup_e:paper") -> list[dict]:
    initial = Decimal(str(get_settings().risk.equity_usd))
    curve = metrics.equity_curve(await _trades(book), initial, await _first_seen(book))
    return [{"t": _ts(t), "v": _num(v, 4)} for t, v in curve]


@app.get("/api/positions")
async def positions(book: str = "setup_e:paper") -> list[dict]:
    now = datetime.now(UTC)
    open_trades = [t for t in await _trades(book) if t.status in ("open", "pending")]
    prices = await _prices([t.symbol for t in open_trades], now)
    out = []
    for t in open_trades:
        row = _trade_row(t)
        quote = prices.get(t.symbol)
        if quote:
            price, as_of = quote
            row |= {
                "price": _num(price),
                "price_as_of": _ts(as_of),
                "unrealized": _num(metrics.unrealized(t, price), 4),
                "to_stop_pct": _num(metrics.stop_distance_pct(t, price), 2),
            }
        out.append(row)
    return out


@app.get("/api/trades")
async def trades(book: str = "setup_e:paper") -> list[dict]:
    return [_trade_row(t) for t in reversed(metrics.closed_trades(await _trades(book)))]


@app.get("/api/journal")
async def journal(
    book: str = "setup_e:paper",
    limit: int = Query(200, ge=1, le=2000),
    include_dry: bool = False,
) -> list[dict]:
    query = select(LiveJournal).where(LiveJournal.setup == book)
    if not include_dry:
        query = query.where(LiveJournal.dry_run.is_(False))
    async with session_scope() as s:
        rows = (
            await s.execute(query.order_by(LiveJournal.id.desc()).limit(limit))
        ).scalars().all()
    return [
        {"ts": _ts(r.ts), "action": r.action, "symbol": r.symbol, "code": r.code,
         "note": r.note, "dry_run": r.dry_run}
        for r in rows
    ]


@app.get("/api/runlog")
async def runlog(blocks: int = Query(30, ge=1, le=500)) -> dict:
    """Human-readable run journal — the latest blocks, newest first."""
    if not JOURNAL.exists():
        return {"path": str(JOURNAL), "blocks": []}
    text = JOURNAL.read_text(encoding="utf-8")
    parts = [b.strip() for b in text.split("\n---\n") if b.strip().startswith("## ")]
    return {"path": str(JOURNAL), "blocks": list(reversed(parts))[:blocks]}


#: Acceptance thresholds (DEC-028) — the same as in `backtest/report.py`. Here
#: for display only: the panel does not make the decision.
ACCEPTANCE = [
    {"name": "Average result (E)", "need": "> +0.15R", "backtest": "+0.024R", "ok": False},
    {"name": "Number of trades", "need": "≥ 60", "backtest": "4,118", "ok": True},
    {"name": "Drawdown p95, portfolio", "need": "< 40%", "backtest": "21.5%", "ok": True},
    {"name": "Survives 0.10% slippage", "need": "E > 0", "backtest": "+0.021R", "ok": True},
    {"name": "Without the two best symbols", "need": "E > 0", "backtest": "+0.012R", "ok": True},
    {"name": "Above a random direction", "need": "≥ 2 SE", "backtest": "+2.63 SE", "ok": True},
]

#: Weeks to 60 closed trades with N slots — median over 20 orderings
#: of simultaneous signals, in-sample 2022-02 — 2026-02 (DEC-050).
FORWARD_PACE_WEEKS = {2: 35, 3: 19, 4: 15, 5: 12}

LAUNCH_AGENT = Path.home() / "Library/LaunchAgents/com.crypto.daily.plist"
PAPER_STATE = Path("var/paper")


@app.get("/api/strategy")
async def strategy(book: str = "setup_e:paper") -> dict:
    settings = get_settings()
    setup = book.split(":", 1)[0]
    cfg = settings.strategy.get(setup, {})
    risk = settings.risk
    initial = Decimal(str(risk.equity_usd))
    trades = await _trades(book)
    st = metrics.stats(trades, initial, await _first_seen(book))
    return {
        "setup": setup,
        "name": cfg.get("name"),
        "params": {
            "lookback_days": cfg.get("lookback_days"),
            "hold_days": cfg.get("hold_days"),
            "stop_atr_mult": cfg.get("stop_atr_mult"),
            "stop_atr_period": cfg.get("stop_atr_period"),
            "max_stop_pct": cfg.get("max_stop_pct"),
        },
        "risk": {
            "equity_usd": risk.equity_usd,
            "risk_per_trade_pct": risk.max_loss_per_trade_pct,
            "max_position_pct": risk.max_position_pct,
            "max_positions": risk.max_concurrent_positions,
            "daily_loss_pct": risk.daily_loss_limit_pct,
            "weekly_loss_pct": risk.weekly_loss_limit_pct,
            "slippage_pct": settings.costs.slippage_pct,
            "taker_fee_pct": settings.costs.taker_fee_pct,
        },
        "acceptance": ACCEPTANCE,
        "backtest": {
            "window": "2022-02-01 — 2026-02-07",
            "trades": 4118,
            "taken_by_portfolio": 359,
            "equity_from": 100.0,
            "equity_to": 135.14,
            # The DEC-039 figures were measured with 2 slots; there are more slots now (DEC-050).
            "slots": 2,
            "verdict": "rejected on the E threshold (DEC-039)",
        },
        "forward": {
            "closed": st.closed,
            "target": st.forward_target,
            "pace_weeks": FORWARD_PACE_WEEKS.get(risk.max_concurrent_positions),
            "avg_r": _num(st.avg_r, 3),
        },
    }


@app.get("/api/health")
async def health(book: str = "setup_e:paper") -> dict:
    from src.live.runner import month_of_decision
    from src.live.signals import decision_moment

    now = datetime.now(UTC)
    # Universe month — as in the cycle: by the signal bar, not by the calendar.
    month = month_of_decision(decision_moment(now))
    async with session_scope() as s:
        frames = dict(
            (await s.execute(
                select(Ohlcv.timeframe, func.max(Ohlcv.ts))
                .where(Ohlcv.timeframe.in_(("1h", "4h")))
                .group_by(Ohlcv.timeframe)
            )).all()
        )
        funding = (await s.execute(select(func.max(FundingHistory.ts)))).scalar()
        from src.core.db import Instrument

        instruments = (await s.execute(select(func.max(Instrument.updated_at)))).scalar()
        universe = (
            await s.execute(
                select(func.count()).where(
                    UniverseSnapshot.month == month, UniverseSnapshot.included.is_(True)
                )
            )
        ).scalar()
    step = {"1h": timedelta(hours=1), "4h": timedelta(hours=4)}
    venue = book.split(":", 1)[-1]
    paper_file = PAPER_STATE / f"{book.split(':', 1)[0]}.json"
    replayed = None
    if venue == "paper" and paper_file.exists():
        import json

        replayed = json.loads(paper_file.read_text()).get("replayed_until")
    done = sorted(Path("var/live").glob("done_*"))
    return {
        "database": True,
        "data": {
            "1h": _ts(frames["1h"] + step["1h"]) if frames.get("1h") else None,
            "4h": _ts(frames["4h"] + step["4h"]) if frames.get("4h") else None,
            "funding": _ts(funding),
        },
        "universe": {"month": month, "symbols": universe},
        "scheduler": {
            "installed": LAUNCH_AGENT.exists(),
            "last_done_day": done[-1].name.removeprefix("done_") if done else None,
        },
        "paper_replayed_until": replayed,
        "instruments_updated": _ts(instruments),
        "now": _ts(now),
    }


@app.exception_handler(ConnectionRefusedError)
@app.exception_handler(OSError)
async def db_down(_request, exc: Exception) -> JSONResponse:  # noqa: ANN001
    return JSONResponse(
        status_code=503,
        content={"detail": f"database unavailable — is Docker running? ({type(exc).__name__})"},
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.include_router(bots_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


__all__ = ["app"]
