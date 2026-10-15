"""Bot platform board: the only channel between the bots. DEC-049.

Each bot records its run — what it received, what it did, what it handed
over — and the path of every candidate through its stage. The next bot reads
the previous one's result from here, the panel — the bot windows and their charts.

Two implementations of one interface: in the DB — for work, in memory — for
testing roles without a DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import delete, select

#: PLATFORM rejection codes (data, window, reconciliation, second key), as opposed
#: to strategy and risk rules. Without the split the sample of 60 forward-test
#: trades is distorted: a rejection due to the Mac sleeping is not a strategy property.
PLATFORM_CODES = frozenset({
    "STALE_DATA", "STALE_SIGNAL", "NO_PRICE", "NO_INSTRUMENT", "PRICE_DIVERGENCE",
    "ENTRY_WINDOW_MISSED", "RISK_NOT_APPROVED", "RISK_MISMATCH", "SIGNAL_MISMATCH",
    "EXCHANGE_ERROR", "MODE_OFF", "NO_UNIVERSE", "ALL_STALE", "HALTED", "DECISION_DONE",
})


def origin_of(code: str | None) -> str:
    return "platform" if code in PLATFORM_CODES else "rule"


@dataclass(frozen=True)
class Trace:
    symbol: str
    stage: str  # signal / risk / execution / verify
    verdict: str  # pass / reject / fail / info
    code: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return origin_of(self.code) if self.verdict != "pass" else "rule"


@dataclass(frozen=True)
class Intent:
    symbol: str
    verdict: str  # approve / reject
    code: str | None = None
    is_long: bool | None = None
    qty: Decimal | None = None
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None


@dataclass(frozen=True)
class RunInfo:
    id: int
    bot: str
    status: str
    summary: str
    outputs: dict
    finished_at: datetime | None


class Board(Protocol):
    async def start_run(self, bot: str, book: str, moment: datetime, shift: str,
                        inputs: dict) -> int: ...

    async def finish_run(self, run_id: int, *, status: str, summary: str, outputs: dict,
                         metrics: dict, error: str | None) -> None: ...

    async def latest_run(self, bot: str, book: str, moment: datetime,
                         shift: str = "night") -> RunInfo | None: ...

    async def add_traces(self, book: str, moment: datetime, run_id: int,
                         traces: list[Trace]) -> None: ...

    async def save_intents(self, book: str, moment: datetime, run_id: int,
                           intents: list[Intent]) -> None: ...

    async def intents(self, book: str, moment: datetime) -> dict[str, Intent]: ...

    async def abandon(self, bot: str, book: str, moment: datetime, shift: str,
                      reason: str) -> int: ...


@dataclass
class MemoryBoard:
    runs: list[dict] = field(default_factory=list)
    traces: list[tuple[str, datetime, int, Trace]] = field(default_factory=list)
    _intents: dict[tuple[str, datetime], dict[str, Intent]] = field(default_factory=dict)

    async def start_run(self, bot, book, moment, shift, inputs) -> int:  # noqa: ANN001
        self.runs.append({"id": len(self.runs) + 1, "bot": bot, "book": book, "moment": moment,
                          "shift": shift, "status": "running", "inputs": inputs,
                          "summary": "", "outputs": {}, "metrics": {}, "finished_at": None})
        return len(self.runs)

    async def finish_run(self, run_id, *, status, summary, outputs, metrics, error) -> None:  # noqa: ANN001
        self.runs[run_id - 1] |= {"status": status, "summary": summary, "outputs": outputs,
                                  "metrics": metrics, "error": error,
                                  "finished_at": datetime.now(UTC)}

    async def latest_run(self, bot, book, moment, shift="night") -> RunInfo | None:  # noqa: ANN001
        for r in reversed(self.runs):
            if (r["bot"], r["book"], r["moment"], r["shift"]) == (bot, book, moment, shift):
                return RunInfo(r["id"], bot, r["status"], r["summary"], r["outputs"],
                               r["finished_at"])
        return None

    async def add_traces(self, book, moment, run_id, traces) -> None:  # noqa: ANN001
        self.traces += [(book, moment, run_id, t) for t in traces]

    async def save_intents(self, book, moment, run_id, intents) -> None:  # noqa: ANN001
        self._intents[(book, moment)] = {i.symbol: i for i in intents}

    async def intents(self, book, moment) -> dict[str, Intent]:  # noqa: ANN001
        return dict(self._intents.get((book, moment), {}))

    async def abandon(self, bot, book, moment, shift, reason) -> int:  # noqa: ANN001
        hit = [r for r in self.runs if (r["bot"], r["book"], r["moment"], r["shift"], r["status"])
               == (bot, book, moment, shift, "running")]
        for r in hit:
            r |= {"status": "failed", "summary": reason, "finished_at": datetime.now(UTC)}
        return len(hit)


class DbBoard:
    async def start_run(self, bot, book, moment, shift, inputs) -> int:  # noqa: ANN001
        from src.core.db import BotRun, session_scope

        async with session_scope() as s:
            row = BotRun(bot=bot, book=book, moment=moment, shift=shift,
                         started_at=datetime.now(UTC), status="running", inputs=inputs,
                         outputs={}, metrics={}, summary="")
            s.add(row)
            await s.flush()
            return row.id

    async def finish_run(self, run_id, *, status, summary, outputs, metrics, error) -> None:  # noqa: ANN001
        from src.core.db import BotRun, session_scope

        async with session_scope() as s:
            row = await s.get(BotRun, run_id)
            row.status, row.summary, row.error = status, summary, error
            row.outputs, row.metrics = outputs, metrics
            row.finished_at = datetime.now(UTC)

    async def latest_run(self, bot, book, moment, shift="night") -> RunInfo | None:  # noqa: ANN001
        from src.core.db import BotRun, session_scope

        async with session_scope() as s:
            row = (
                await s.execute(
                    select(BotRun)
                    .where(BotRun.bot == bot, BotRun.book == book, BotRun.moment == moment,
                           BotRun.shift == shift)
                    .order_by(BotRun.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return RunInfo(row.id, row.bot, row.status, row.summary, row.outputs or {},
                           row.finished_at)

    async def add_traces(self, book, moment, run_id, traces) -> None:  # noqa: ANN001
        from src.core.db import DecisionTrace, session_scope

        if not traces:
            return
        now = datetime.now(UTC)
        async with session_scope() as s:
            s.add_all([
                DecisionTrace(book=book, moment=moment, symbol=t.symbol, stage=t.stage,
                              verdict=t.verdict, code=t.code, origin=t.origin,
                              detail=t.detail, bot_run_id=run_id, ts=now)
                for t in traces
            ])

    async def save_intents(self, book, moment, run_id, intents) -> None:  # noqa: ANN001
        """The day's approvals are replaced wholesale: a Risk Manager retry on the
        same day recomputes them by the same rule."""
        from src.core.db import OrderIntent, session_scope

        now = datetime.now(UTC)
        async with session_scope() as s:
            await s.execute(delete(OrderIntent).where(OrderIntent.book == book,
                                                      OrderIntent.moment == moment))
            s.add_all([
                OrderIntent(book=book, moment=moment, symbol=i.symbol, verdict=i.verdict,
                            code=i.code, is_long=i.is_long, qty=i.qty,
                            entry_price=i.entry_price, stop_price=i.stop_price,
                            bot_run_id=run_id, created_at=now)
                for i in intents
            ])

    async def intents(self, book, moment) -> dict[str, Intent]:  # noqa: ANN001
        from src.core.db import OrderIntent, session_scope

        async with session_scope() as s:
            rows = (
                await s.execute(select(OrderIntent).where(OrderIntent.book == book,
                                                          OrderIntent.moment == moment))
            ).scalars().all()
        return {
            r.symbol: Intent(r.symbol, r.verdict, r.code, r.is_long, r.qty, r.entry_price,
                             r.stop_price)
            for r in rows
        }

    async def abandon(self, bot, book, moment, shift, reason) -> int:  # noqa: ANN001
        """A run whose process was killed on timeout or died before writing its
        result: otherwise it would stay "running" forever."""
        from src.core.db import BotRun, session_scope

        async with session_scope() as s:
            rows = (await s.execute(
                select(BotRun).where(BotRun.bot == bot, BotRun.book == book,
                                     BotRun.moment == moment, BotRun.shift == shift,
                                     BotRun.status == "running")
            )).scalars().all()
            for row in rows:
                row.status, row.summary, row.error = "failed", reason, reason
                row.finished_at = datetime.now(UTC)
            return len(rows)


__all__ = [
    "PLATFORM_CODES",
    "Board",
    "DbBoard",
    "Intent",
    "MemoryBoard",
    "RunInfo",
    "Trace",
    "origin_of",
]
