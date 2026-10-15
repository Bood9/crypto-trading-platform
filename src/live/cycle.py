"""The daily trading cycle. Phase 5, DEC-041.

One run — one decision on the UTC daily close. The order of the steps is
part of the contract:

1. **Reconciliation with the exchange.** First we find out what really exists. A position
   that is gone is explained only by a fill of its own stop
   confirmed by the exchange; anything else unexplained disables
   trading (P-001). A position without a stop gets a stop immediately —
   and trading is disabled too.
2. **Time exits.** Before entries, as in the backtest: a position closed
   today frees a slot for today's signal.
3. **Entries.** Candidates in the same order as in the portfolio replay;
   each goes through the sizer and the governor on the ACTUAL equity.

**Equity is the accounting one, not the exchange balance.** The deposit is set in the config
(DEC-038) and lives its own life: the starting amount plus the realized
result from fills. On testnet the balance is toy tens of thousands,
and a size derived from it would describe a different account.

**A rerun is safe.** Order keys are derived from the symbol
and the decision moment, so a second run on the same day gets
a "duplicate" from the exchange rather than a second position; the trade is recorded BEFORE
the entry is sent.

The module does not touch the network or the database itself: the exchange, the store,
correlations and funding come in as arguments. So the cycle is tested in full
on the paper exchange.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal

from src.backtest.engine import Signal
from src.execution.exchange import Fill, TradingVenue
from src.execution.executor import close_by_time, open_position
from src.execution.plan import (
    Order,
    OrderKind,
    OrderPlan,
    PlanRejected,
    build,
    emergency_close,
    link_id,
    time_close,
)
from src.execution.reconcile import LocalPosition, reconcile
from src.live.signals import Candidate, decision_moment
from src.live.store import JournalEntry, LiveTrade, Store
from src.risk.governor import (
    GovernorLimits,
    HeldPosition,
    PortfolioState,
    TradingMode,
    check,
)
from src.risk.position_sizer import SizingInput, calculate_size

#: Tolerance for comparing fill and position quantities.
QTY_TOLERANCE = Decimal("1e-12")

CorrelationAt = Callable[[str, str, datetime], Awaitable[Decimal | None]]
#: Sum of funding rates for the symbol strictly after `start` and no later than `end`.
FundingRates = Callable[[str, datetime, datetime], Awaitable[Decimal]]


@dataclass(frozen=True)
class Sizing:
    """Sizer parameters from config.risk. MMR — per symbol, from the API cache."""

    margin_pct: Decimal
    max_loss_pct: Decimal
    leverage_cap: Decimal
    min_liq_buffer_ratio: Decimal
    max_position_pct: Decimal | None

    @classmethod
    def from_config(cls, risk) -> Sizing:  # noqa: ANN001
        return cls(
            margin_pct=Decimal(str(risk.margin_per_trade_pct)),
            max_loss_pct=Decimal(str(risk.max_loss_per_trade_pct)),
            leverage_cap=Decimal(str(risk.leverage_cap)),
            min_liq_buffer_ratio=Decimal(str(risk.min_liq_buffer_ratio)),
            max_position_pct=(
                Decimal(str(risk.max_position_pct))
                if risk.max_position_pct is not None
                else None
            ),
        )


@dataclass
class CycleContext:
    setup: str
    venue: TradingVenue
    store: Store
    limits: GovernorLimits
    sizing: Sizing
    initial_equity: Decimal
    hold: timedelta
    #: Candidates already in queue order (`signals.ordered`).
    candidates: list[Candidate]
    correlation: CorrelationAt
    funding_rates: FundingRates
    now: datetime
    dry_run: bool = True
    #: How far the execution exchange price may differ from the close
    #: the signal was computed on. More — the signal was computed on a different
    #: market or on a stale price (P-001).
    max_divergence_pct: Decimal = Decimal("3")
    mmr: dict[str, Decimal] = field(default_factory=dict)
    default_mmr: Decimal = Decimal("0.005")
    #: How long after UTC midnight entering is still allowed. The backtest enters
    #: on the daily close; an entry nine hours later is already a different trade
    #: that nobody has tested. Exits do not depend on the window.
    entry_window: timedelta = timedelta(hours=2)
    #: A live clock for the real exchange: the entry window is checked against it right
    #: before the order, not against `now` frozen at the cycle start (the Mac could
    #: fall asleep in the middle of the candidates). None for paper — its execution
    #: moment is set by the decision (`paper_as_of`).
    clock: Callable[[], datetime] | None = None
    #: Second key — Risk Manager approval (DEC-049). None — without it.
    gate: EntryGate | None = None


@dataclass(frozen=True)
class PlannedEntry:
    """The entry plan as the cycle saw it: for the second key (DEC-049)."""

    symbol: str
    is_long: bool
    qty: Decimal
    entry_price: Decimal
    stop_price: Decimal


#: Second key: a rejection code or None — "allowed". Called right before the
#: write-ahead record; one "no" is enough, "yes" is needed from both.
EntryGate = Callable[[PlannedEntry], Awaitable[str | None]]


@dataclass
class CycleReport:
    moment: datetime
    dry_run: bool
    halted: bool = False
    opened: list[str] = field(default_factory=list)
    #: Plans of entries that reached the order (in preview — up to WOULD_OPEN).
    planned: list[PlannedEntry] = field(default_factory=list)
    closed: list[tuple[str, str, Decimal]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    equity: Decimal = Decimal(0)
    mode: TradingMode = TradingMode.ON


def plan_of(trade: LiveTrade) -> OrderPlan:
    """The trade's order plan, restored from the record.

    The Executor needs it for closing: what matters to it is the symbol, the side,
    the quantity and the order keys — all of that is in the record.
    """

    def order(kind: OrderKind, link: str, *, buy: bool, price: Decimal | None,
              reduce_only: bool) -> Order:
        return Order(kind=kind, symbol=trade.symbol, buy=buy, qty=trade.qty,
                     price=price, reduce_only=reduce_only, link_id=link)

    return OrderPlan(
        symbol=trade.symbol,
        is_long=trade.is_long,
        entry=order(OrderKind.ENTRY, trade.entry_link_id, buy=trade.is_long,
                    price=None, reduce_only=False),
        stop_loss=order(OrderKind.STOP_LOSS, trade.stop_link_id, buy=not trade.is_long,
                        price=trade.stop_price, reduce_only=True),
        take_profit=None,
        entry_price=trade.entry_price,
        stop_price=trade.stop_price,
        target_price=None,
        qty=trade.qty,
        notional=trade.entry_price * trade.qty,
    )


def local_of(trade: LiveTrade) -> LocalPosition:
    return LocalPosition(
        symbol=trade.symbol,
        is_long=trade.is_long,
        qty=trade.qty,
        stop_link_id=trade.stop_link_id,
        target_link_id=trade.target_link_id,
    )


def realized(trade: LiveTrade, exit_fill: Fill, funding_sum: Decimal) -> Decimal:
    """Result from fills: price, both fees and funding.

    Funding is signed, as in the backtest: a positive rate is charged
    to a long and credited to a short (DEC-025).
    """
    direction = Decimal(1) if trade.is_long else Decimal(-1)
    price_pnl = (exit_fill.avg_price - trade.entry_price) * trade.qty * direction
    funding_paid = funding_sum * direction * trade.entry_price * trade.qty
    return price_pnl - trade.entry_fee - exit_fill.fee - funding_paid


def _n(value: Decimal) -> str:
    """A number without trailing zeros and without an exponent — for the journal."""
    return format(value.normalize(), "f")


class Cycle:
    def __init__(self, ctx: CycleContext) -> None:
        self.ctx = ctx
        self.moment = decision_moment(ctx.now)
        self.report = CycleReport(moment=self.moment, dry_run=ctx.dry_run)
        self.state: PortfolioState | None = None
        self.trades: list[LiveTrade] = []

    # -- journal --------------------------------------------------------
    async def note(self, action: str, *, symbol: str | None = None,
                   code: str | None = None, text: str = "") -> None:
        await self.ctx.store.journal(
            JournalEntry(ts=self.ctx.now, setup=self.ctx.setup, action=action,
                         symbol=symbol, code=code, note=text, dry_run=self.ctx.dry_run)
        )
        if text:
            self.report.notes.append(f"{action} {symbol or ''}: {text}".strip())

    async def halt(self, reason: str) -> CycleReport:
        assert self.state is not None
        self.state.mode = TradingMode.OFF_MANUAL
        self.report.halted = True
        await self.note("HALT", code="MODE_OFF", text=reason)
        return await self.finish()

    async def finish(self) -> CycleReport:
        assert self.state is not None
        if not self.ctx.dry_run:
            await self.ctx.store.save_state(self.ctx.setup, self.state)
        self.report.equity = self.state.equity
        self.report.mode = self.state.mode
        return self.report

    # -- accounting -----------------------------------------------------
    async def book_close(self, trade: LiveTrade, fill: Fill, reason: str) -> None:
        assert self.state is not None
        funding = await self.ctx.funding_rates(trade.symbol, trade.opened_at, self.ctx.now)
        pnl = realized(trade, fill, funding)
        closed = replace(trade, status="closed", closed_at=self.ctx.now,
                         close_reason=reason, exit_price=fill.avg_price, pnl=pnl)
        await self.ctx.store.update_trade(closed)
        self.state.positions.pop(trade.symbol, None)
        self.state.apply_pnl(pnl, self.ctx.now)
        # Equity goes to disk right after a closed trade, not at the end of the cycle:
        # a later failure (database, exchange, disk) would leave the trade closed and its
        # result outside equity forever: a rerun no longer sees it (core
        # review 2026-09-30; the Controller re-checks the accounting).
        await self.ctx.store.save_state(self.ctx.setup, self.state)
        self.trades = [t for t in self.trades if t.id != trade.id]
        self.report.closed.append((trade.symbol, reason, pnl))
        await self.note("CLOSED", symbol=trade.symbol, code=reason,
                        text=f"exit {_n(fill.avg_price)}, result {pnl:+.4f}")

    def hold(self, trade: LiveTrade) -> None:
        assert self.state is not None
        self.state.positions[trade.symbol] = HeldPosition(
            symbol=trade.symbol, is_long=trade.is_long, margin=trade.margin,
            qty=trade.qty, entry=trade.entry_price, opened_at=trade.opened_at,
        )

    # -- steps ----------------------------------------------------------
    async def run(self) -> CycleReport:
        ctx = self.ctx
        self.state = await ctx.store.load_state(ctx.setup) or PortfolioState.start(
            ctx.initial_equity, ctx.now
        )
        self.trades = await ctx.store.live_trades(ctx.setup)

        await self.settle_vanished()
        for trade in self.trades:
            self.hold(trade)

        if not await self.reconciled():
            return self.report if self.report.halted else await self.halt("reconciliation")

        if not await self.time_exits():
            return await self.halt("time close not confirmed")

        self.state.roll(ctx.now)
        if not self.state.mode.trading_allowed:
            # Without candidates (day watch) there is nothing to note: a line
            # every hour would push the halt reason out of trade-status.
            if ctx.candidates:
                await self.note("NO_ENTRIES", code=self.state.mode.value,
                                text="trading disabled — no entries, exits done")
            return await self.finish()

        await self.entries()
        return await self.finish()

    async def settle_vanished(self) -> None:
        """Trades that are not on the exchange — explain them by fills.

        An unexplained trade stays in the list: reconciliation will see it as a
        phantom and disable trading. Nothing is disabled here —
        reconciliation decides, on the full snapshot.
        """
        ctx = self.ctx
        held = {p.symbol: p for p in await ctx.venue.positions()}
        for trade in list(self.trades):
            if trade.status == "pending":
                # The process crashed between the record and the entry outcome.
                entry = await ctx.venue.fill(trade.entry_link_id, trade.symbol)
                if entry is None and trade.symbol not in held:
                    if not ctx.dry_run:
                        await ctx.store.update_trade(replace(trade, status="void"))
                    self.trades.remove(trade)
                    await self.note("VOID", symbol=trade.symbol, code="NO_FILL",
                                    text="entry never executed — record voided")
                    continue
                if entry is not None:
                    trade = self._promote(trade, entry)
                    if not ctx.dry_run:
                        await ctx.store.update_trade(trade)
            elif trade.entry_fee == 0:
                # The exchange may not have returned the entry fill right away.
                entry = await ctx.venue.fill(trade.entry_link_id, trade.symbol)
                if entry is not None:
                    trade = self._promote(trade, entry)
                    if not ctx.dry_run:
                        await ctx.store.update_trade(trade)

            if trade.symbol in held:
                continue
            stop = await ctx.venue.fill(trade.stop_link_id, trade.symbol)
            if stop is None or abs(stop.qty - trade.qty) > QTY_TOLERANCE:
                continue  # reconciliation will show a phantom
            if ctx.dry_run:
                await self.note("WOULD_BOOK_STOP", symbol=trade.symbol,
                                text=f"stop filled at {stop.avg_price}")
                self.trades.remove(trade)
                continue
            await self.book_close(trade, stop, "STOP")

    def _promote(self, trade: LiveTrade, entry: Fill) -> LiveTrade:
        promoted = replace(trade, status="open", entry_price=entry.avg_price,
                           entry_fee=entry.fee)
        self.trades = [promoted if t.id == trade.id else t for t in self.trades]
        return promoted

    async def reconciled(self) -> bool:
        ctx = self.ctx
        result = reconcile(
            [local_of(t) for t in self.trades],
            await ctx.venue.positions(),
            await ctx.venue.open_orders(),
        )
        if not result.findings:
            return True

        for finding in result.to_cancel:
            await self.note("CANCEL_ORPHAN", symbol=finding.symbol,
                            code=finding.kind.value, text=finding.note)
        if not ctx.dry_run:
            orphan_links = {
                o.link_id for o in await ctx.venue.open_orders()
            } - {link for t in self.trades for link in local_of(t).expected_orders}
            for link in sorted(orphan_links):
                await ctx.venue.cancel(link)

        for finding in result.unprotected:
            trade = next(t for t in self.trades if t.symbol == finding.symbol)
            await self.protect(trade)

        if result.halt_required:
            await self.halt(result.describe())
            return False
        return True

    async def protect(self, trade: LiveTrade) -> None:
        """A new stop at the trade level. The key is derived from the repair moment:
        the exchange may already have seen the old key, and a retry with it would be
        taken for a duplicate."""
        ctx = self.ctx
        order = Order(
            kind=OrderKind.STOP_LOSS, symbol=trade.symbol, buy=not trade.is_long,
            qty=trade.qty, price=trade.stop_price, reduce_only=True,
            link_id=link_id(trade.symbol, ctx.now, OrderKind.STOP_LOSS),
        )
        if ctx.dry_run:
            await self.note("WOULD_PLACE_STOP", symbol=trade.symbol, code="UNPROTECTED")
            return
        try:
            await ctx.venue.place(order)
        except Exception as exc:  # noqa: BLE001 — the outcome is checked on the exchange below
            await self.note("STOP_REPAIR_ERROR", symbol=trade.symbol, text=str(exc))
        live = {o.link_id for o in await ctx.venue.open_orders()}
        if order.link_id in live:
            await ctx.store.update_trade(replace(trade, stop_link_id=order.link_id))
            await self.note("STOP_PLACED", symbol=trade.symbol, code="UNPROTECTED")
        else:
            await self.note("STOP_REPAIR_FAILED", symbol=trade.symbol, code="UNPROTECTED",
                            text="POSITION WITHOUT A STOP — a human is needed")

    async def time_exits(self) -> bool:
        ctx = self.ctx
        for trade in list(self.trades):
            if trade.close_due_at > self.moment:
                continue
            if ctx.dry_run:
                await self.note("WOULD_CLOSE", symbol=trade.symbol, code="TIME_STOP")
                self.trades.remove(trade)
                self.state.positions.pop(trade.symbol, None)
                continue
            plan = plan_of(trade)
            closing = await close_by_time(ctx.venue, plan, moment=self.moment)
            if not closing.closed:
                for n in closing.notes:
                    await self.note("CLOSE_FAILED", symbol=trade.symbol, text=n)
                return False
            fill = await ctx.venue.fill(time_close(plan, self.moment).link_id, trade.symbol)
            reason = "TIME_STOP"
            if fill is None:
                # The stop closed the position between reconciliation and the close.
                fill, reason = await ctx.venue.fill(trade.stop_link_id, trade.symbol), "STOP"
            if fill is None:
                await self.note("CLOSE_UNPRICED", symbol=trade.symbol,
                                text="position closed, fills not found")
                return False
            await self.book_close(trade, fill, reason)
        return True

    async def entries(self) -> None:
        ctx = self.ctx
        for cand in ctx.candidates:
            code, text = await self.try_enter(cand)
            if code is not None:
                self.report.skipped.append((cand.symbol, code))
                await self.note("SKIP", symbol=cand.symbol, code=code, text=text)
            if self.report.halted:
                return

    async def try_enter(self, cand: Candidate) -> tuple[str | None, str]:
        ctx = self.ctx
        state = self.state
        assert state is not None
        if cand.bar_ts != self.moment - timedelta(days=1):
            return "STALE_SIGNAL", f"signal on the bar of {cand.bar_ts:%Y-%m-%d}"
        late = (ctx.clock() if ctx.clock else ctx.now) - self.moment
        if late > ctx.entry_window:
            minutes = int(late.total_seconds() // 60)
            window = int(ctx.entry_window.total_seconds() // 3600)
            return "ENTRY_WINDOW_MISSED", (
                f"run {minutes // 60} h {minutes % 60} min after the daily close, "
                f"entry window is {window} h"
            )
        if cand.symbol in state.positions:
            return "MAX_POSITIONS", "position already open"

        price = await ctx.venue.last_price(cand.symbol)
        if price is None:
            return "NO_PRICE", "the exchange gave no price"
        divergence = abs(price / cand.close - 1) * 100
        if divergence > ctx.max_divergence_pct:
            return "PRICE_DIVERGENCE", (
                f"price {price} vs close {cand.close}: {divergence:.2f}% > "
                f"{ctx.max_divergence_pct}%"
            )
        spec = await ctx.venue.instrument(cand.symbol)
        if spec is None:
            return "NO_INSTRUMENT", "instrument not trading on the execution exchange"

        f = cand.stop_fraction
        stop = price * (1 - f) if cand.is_long else price * (1 + f)
        sized = calculate_size(
            SizingInput(
                equity=state.equity, entry_price=price, stop_price=stop,
                mmr=ctx.mmr.get(cand.symbol, ctx.default_mmr),
                qty_step=spec.qty_step, min_order_qty=spec.min_order_qty,
                min_notional=spec.min_notional,
                margin_pct=ctx.sizing.margin_pct, max_loss_pct=ctx.sizing.max_loss_pct,
                leverage_cap=ctx.sizing.leverage_cap,
                min_liq_buffer_ratio=ctx.sizing.min_liq_buffer_ratio,
                max_position_pct=ctx.sizing.max_position_pct,
            )
        )
        if not sized.approved:
            return (sized.rejection.value if sized.rejection else "SIZER"), sized.note or ""

        known = {h: await ctx.correlation(h, cand.symbol, cand.bar_ts) for h in state.positions}
        verdict = check(
            state, ctx.limits, symbol=cand.symbol, is_long=cand.is_long,
            margin=sized.margin, moment=ctx.now, correlation=lambda a, _b: known.get(a),
        )
        if not verdict.approved:
            code = verdict.rejection.value if verdict.rejection else "GOVERNOR"
            return code, verdict.note or ""

        plan = build(Signal(is_long=cand.is_long, entry=price, stop=stop, target=None),
                     spec=spec, qty=sized.qty, moment=self.moment)
        if isinstance(plan, PlanRejected):
            return plan.rejection.value, plan.note

        trade = LiveTrade(
            setup=ctx.setup, symbol=cand.symbol, is_long=cand.is_long, qty=plan.qty,
            entry_price=plan.entry_price, stop_price=plan.stop_price, margin=sized.margin,
            entry_link_id=plan.entry.link_id, stop_link_id=plan.stop_loss.link_id,
            bar_ts=cand.bar_ts, opened_at=self.moment,
            close_due_at=self.moment + ctx.hold,
        )
        side = "long" if cand.is_long else "short"
        summary = (f"{side} {_n(plan.qty)} at ~{_n(plan.entry_price)}, stop {_n(plan.stop_price)}, "
                   f"notional {plan.notional:.2f}, risk {sized.loss_at_stop:.2f}")
        planned = PlannedEntry(cand.symbol, cand.is_long, plan.qty, plan.entry_price,
                               plan.stop_price)
        if ctx.gate is not None:
            refusal = await ctx.gate(planned)
            if refusal is not None:
                return refusal, "second key did not allow the entry"
        self.report.planned.append(planned)
        if ctx.dry_run:
            self.hold(trade)
            self.report.opened.append(cand.symbol)
            await self.note("WOULD_OPEN", symbol=cand.symbol, text=summary)
            return None, ""

        # Write-ahead record: the trade is on disk BEFORE the entry is sent.
        if ctx.clock is not None and ctx.clock() - self.moment > ctx.entry_window:
            return "ENTRY_WINDOW_MISSED", "entry window closed while the order was being prepared"
        trade = await ctx.store.add_trade(trade)
        report = await open_position(
            ctx.venue, plan, moment=self.moment, held=[local_of(t) for t in self.trades]
        )
        if report.entered and report.protected:
            entry = await ctx.venue.fill(plan.entry.link_id, cand.symbol)
            trade = replace(trade, status="open")
            if entry is not None:
                trade = replace(trade, entry_price=entry.avg_price, entry_fee=entry.fee)
            await ctx.store.update_trade(trade)
            self.trades.append(trade)
            self.hold(trade)
            self.report.opened.append(cand.symbol)
            await self.note("OPENED", symbol=cand.symbol, text=summary)
        elif not report.entered:
            await ctx.store.update_trade(replace(trade, status="void"))
            await self.note("ENTRY_FAILED", symbol=cand.symbol, code="EXCHANGE_ERROR",
                            text="; ".join(report.notes))
        else:
            # There was an entry, the stop did not go in — the Executor closed at market.
            # The loss of this pair of orders is real and must reach equity.
            await self.note("EMERGENCY_CLOSE", symbol=cand.symbol,
                            text="; ".join(report.notes))
            entry = await ctx.venue.fill(plan.entry.link_id, cand.symbol)
            exit_fill = await ctx.venue.fill(
                emergency_close(plan, self.moment).link_id, cand.symbol
            )
            if entry is not None:
                trade = replace(trade, status="open", entry_price=entry.avg_price,
                                entry_fee=entry.fee)
            if entry is not None and exit_fill is not None and report.closed_by_emergency:
                await self.book_close(trade, exit_fill, "EMERGENCY")
            else:
                # Not closed or not valued — the record stays live,
                # and the next reconciliation will show what is on the exchange.
                await ctx.store.update_trade(replace(trade, status="open"))
        if report.halt:
            await self.halt(
                report.reconciliation.describe() if report.reconciliation else "execution"
            )
        return None, ""


async def run_cycle(ctx: CycleContext) -> CycleReport:
    return await Cycle(ctx).run()


__all__ = [
    "EntryGate",
    "PlannedEntry",
    "Cycle",
    "CycleContext",
    "CycleReport",
    "Sizing",
    "local_of",
    "plan_of",
    "realized",
    "run_cycle",
]
