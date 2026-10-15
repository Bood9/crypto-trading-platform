"""The daily cycle's memory between runs. Phase 5, DEC-041.

Two implementations of one interface: in memory — for cycle tests,
in the database — for operation. The cycle does not know which one it has, and so it is
tested in full without a database.

What is stored, and why not in process memory:

* **trades** — which positions are ours and under which keys their orders are.
  The record is made BEFORE the entry is sent (status `pending`): the process may
  crash between the exchange response and the record, and then reconciliation would see
  an ownerless position on the exchange;
* **mode and loss reference points** — the weekly stop is lifted only
  by hand (spec §5) and must survive a restart;
* **journal** — every decision with a reason code (P-001).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select

from src.risk.governor import Anchors, PortfolioState, TradingMode

#: Trade statuses that are reconciled with the exchange.
LIVE_STATUSES = ("pending", "open")

#: Mode severity. "Sticky" modes (weekly limit, halt until a
#: human) cannot be lifted by an ordinary save — only by `resume`:
#: otherwise a cycle that started before the halt would bring back ON when saving
#: (a race on writing the whole row, platform design 2026-09-29).
_SEVERITY = {
    TradingMode.ON: 0,
    TradingMode.OFF_DAILY: 1,
    TradingMode.OFF_WEEKLY: 2,
    TradingMode.OFF_MANUAL: 3,
}
_STICKY = 2


def sticky_mode(stored: TradingMode | None, new: TradingMode) -> TradingMode:
    """The mode after saving: a sticky saved mode is not downgraded."""
    if stored is not None and _SEVERITY[stored] >= _STICKY and _SEVERITY[new] < _SEVERITY[stored]:
        return stored
    return new


@dataclass
class LiveTrade:
    setup: str
    symbol: str
    is_long: bool
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    margin: Decimal
    entry_link_id: str
    stop_link_id: str
    bar_ts: datetime
    opened_at: datetime
    close_due_at: datetime
    target_link_id: str | None = None
    entry_fee: Decimal = Decimal(0)
    status: str = "pending"
    closed_at: datetime | None = None
    close_reason: str | None = None
    exit_price: Decimal | None = None
    pnl: Decimal | None = None
    id: int | None = None


@dataclass(frozen=True)
class JournalEntry:
    ts: datetime
    setup: str
    action: str
    symbol: str | None = None
    code: str | None = None
    note: str = ""
    dry_run: bool = True


class Store(Protocol):
    async def load_state(self, setup: str) -> PortfolioState | None: ...

    async def save_state(self, setup: str, state: PortfolioState) -> None: ...

    async def live_trades(self, setup: str) -> list[LiveTrade]: ...

    async def add_trade(self, trade: LiveTrade) -> LiveTrade: ...

    async def update_trade(self, trade: LiveTrade) -> None: ...

    async def journal(self, entry: JournalEntry) -> None: ...

    async def resume(self, setup: str, moment: datetime) -> TradingMode | None:
        """Manual enable: mode ON, reference points — at the current equity
        FROM THE DATABASE. Equity is not overwritten: only trade accounting changes it."""
        ...


@dataclass
class MemoryStore:
    """An in-memory store. Copies on input and output: the cycle must not
    change what is saved without calling save — otherwise a test could not tell
    "wrote" from "changed the object by reference"."""

    states: dict[str, PortfolioState] = field(default_factory=dict)
    trades: list[LiveTrade] = field(default_factory=list)
    entries: list[JournalEntry] = field(default_factory=list)

    async def load_state(self, setup: str) -> PortfolioState | None:
        state = self.states.get(setup)
        if state is None:
            return None
        return PortfolioState(equity=state.equity, anchors=state.anchors, mode=state.mode)

    async def save_state(self, setup: str, state: PortfolioState) -> None:
        stored = self.states.get(setup)
        self.states[setup] = PortfolioState(
            equity=state.equity, anchors=state.anchors,
            mode=sticky_mode(stored.mode if stored else None, state.mode),
        )

    async def resume(self, setup: str, moment: datetime) -> TradingMode | None:
        stored = self.states.get(setup)
        if stored is None:
            return None
        self.states[setup] = PortfolioState(
            equity=stored.equity, anchors=Anchors.at(moment, stored.equity), mode=TradingMode.ON
        )
        return stored.mode

    async def live_trades(self, setup: str) -> list[LiveTrade]:
        return [
            replace(t) for t in self.trades if t.setup == setup and t.status in LIVE_STATUSES
        ]

    async def add_trade(self, trade: LiveTrade) -> LiveTrade:
        for i, t in enumerate(self.trades):
            if t.entry_link_id == trade.entry_link_id:
                if t.status != "void":
                    raise ValueError(f"entry key {trade.entry_link_id} taken by trade {t.id}")
                self.trades[i] = replace(trade, id=t.id)  # retry after a failure: revive
                return replace(self.trades[i])
        stored = replace(trade, id=len(self.trades) + 1)
        self.trades.append(stored)
        return replace(stored)

    async def update_trade(self, trade: LiveTrade) -> None:
        for i, t in enumerate(self.trades):
            if t.id == trade.id:
                self.trades[i] = replace(trade)
                return
        raise KeyError(f"trade {trade.id} not found")

    async def journal(self, entry: JournalEntry) -> None:
        self.entries.append(entry)


class ReadOnlyStore:
    """Reads from the real store, writes to memory (DEC-049).

    For the Risk Manager's rehearsal: it sees the book as is, but
    changes nothing in it — neither the journal, nor trades, nor the state.
    """

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.memory = MemoryStore()

    async def load_state(self, setup: str) -> PortfolioState | None:
        return await self.memory.load_state(setup) or await self._inner.load_state(setup)

    async def save_state(self, setup: str, state: PortfolioState) -> None:
        await self.memory.save_state(setup, state)

    async def live_trades(self, setup: str) -> list[LiveTrade]:
        return await self._inner.live_trades(setup)

    async def add_trade(self, trade: LiveTrade) -> LiveTrade:
        return await self.memory.add_trade(trade)

    async def update_trade(self, trade: LiveTrade) -> None:
        return None

    async def journal(self, entry: JournalEntry) -> None:
        await self.memory.journal(entry)

    async def resume(self, setup: str, moment: datetime) -> TradingMode | None:
        raise PermissionError("rehearsal does not enable trading")


def _week_key(week: tuple[int, int]) -> str:
    return f"{week[0]:04d}-W{week[1]:02d}"


def _week_of(key: str) -> tuple[int, int]:
    year, week = key.split("-W")
    return int(year), int(week)


class DbStore:
    """A database store. Each operation is its own transaction: the trade
    record made before the entry is sent must be on disk BEFORE sending."""

    async def load_state(self, setup: str) -> PortfolioState | None:
        from src.core.db import LiveState, session_scope

        async with session_scope() as s:
            row = await s.get(LiveState, setup)
            if row is None:
                return None
            return PortfolioState(
                equity=row.equity,
                anchors=Anchors(
                    day=date.fromisoformat(row.day),
                    day_equity=row.day_equity,
                    week=_week_of(row.week),
                    week_equity=row.week_equity,
                ),
                mode=TradingMode(row.mode),
            )

    async def save_state(self, setup: str, state: PortfolioState) -> None:
        from src.core.db import LiveState, session_scope

        async with session_scope() as s:
            row = await s.get(LiveState, setup, with_for_update=True)
            if row is None:
                row = LiveState(setup=setup)
                s.add(row)
                stored = None
            else:
                stored = TradingMode(row.mode)
            row.mode = sticky_mode(stored, state.mode).value
            row.equity = state.equity
            row.day = state.anchors.day.isoformat()
            row.day_equity = state.anchors.day_equity
            row.week = _week_key(state.anchors.week)
            row.week_equity = state.anchors.week_equity

    async def live_trades(self, setup: str) -> list[LiveTrade]:
        from src.core.db import LivePosition, session_scope

        async with session_scope() as s:
            rows = (
                await s.execute(
                    select(LivePosition)
                    .where(
                        LivePosition.setup == setup,
                        LivePosition.status.in_(LIVE_STATUSES),
                    )
                    .order_by(LivePosition.id)
                )
            ).scalars().all()
            return [row_to_trade(r) for r in rows]

    async def add_trade(self, trade: LiveTrade) -> LiveTrade:
        """Write-ahead record of the entry.

        The entry key is deterministic (symbol + decision moment). If the process
        crashed after the record and the entry did not happen, the record is voided (`void`), and
        a retry of the same decision must REVIVE it: a new row with the same
        key would hit UNIQUE (platform design 2026-09-29).
        """
        from src.core.db import LivePosition, session_scope

        async with session_scope() as s:
            row = (
                await s.execute(
                    select(LivePosition).where(LivePosition.entry_link_id == trade.entry_link_id)
                )
            ).scalar_one_or_none()
            if row is not None and row.status != "void":
                raise ValueError(f"entry key {trade.entry_link_id} taken by trade {row.id}")
            if row is None:
                row = LivePosition()
                s.add(row)
            _fill_row(row, trade)
            await s.flush()
            return replace(trade, id=row.id)

    async def resume(self, setup: str, moment: datetime) -> TradingMode | None:
        from src.core.db import LiveState, session_scope

        async with session_scope() as s:
            row = await s.get(LiveState, setup, with_for_update=True)
            if row is None:
                return None
            previous = TradingMode(row.mode)
            anchors = Anchors.at(moment, row.equity)
            row.mode = TradingMode.ON.value
            row.day = anchors.day.isoformat()
            row.day_equity = row.equity
            row.week = _week_key(anchors.week)
            row.week_equity = row.equity
            return previous

    async def update_trade(self, trade: LiveTrade) -> None:
        from src.core.db import LivePosition, session_scope

        async with session_scope() as s:
            row = await s.get(LivePosition, trade.id)
            if row is None:
                raise KeyError(f"trade {trade.id} not found")
            _fill_row(row, trade)

    async def journal(self, entry: JournalEntry) -> None:
        from src.core.db import LiveJournal, session_scope

        async with session_scope() as s:
            s.add(
                LiveJournal(
                    ts=entry.ts,
                    setup=entry.setup,
                    symbol=entry.symbol,
                    action=entry.action,
                    code=entry.code,
                    note=entry.note,
                    dry_run=entry.dry_run,
                )
            )


_FIELDS = (
    "setup", "symbol", "is_long", "qty", "entry_price", "stop_price", "margin",
    "entry_fee", "entry_link_id", "stop_link_id", "target_link_id", "bar_ts",
    "opened_at", "close_due_at", "status", "closed_at", "close_reason",
    "exit_price", "pnl",
)


def _fill_row(row, trade: LiveTrade) -> None:  # noqa: ANN001
    for name in _FIELDS:
        setattr(row, name, getattr(trade, name))


def row_to_trade(row) -> LiveTrade:  # noqa: ANN001
    return LiveTrade(id=row.id, **{name: getattr(row, name) for name in _FIELDS})


__all__ = [
    "LIVE_STATUSES",
    "DbStore",
    "JournalEntry",
    "LiveTrade",
    "MemoryStore",
    "ReadOnlyStore",
    "Store",
    "row_to_trade",
]
