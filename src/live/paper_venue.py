"""A paper exchange on main-market prices. Phase 5, DEC-041.

**Why, if there is testnet.** Testnet tests the mechanics: signatures,
formats, idempotency keys, conditional stops. But its prices are its own —
a thin market unrelated to the main one. Trade results
on testnet say nothing about the rule.

Here it is the other way round: execution is paper, but prices are real, from the collected
Bybit hourly bars. This is an out-of-sample (OOS) forward test: the rule
is frozen, data arrives after the freeze, and there is nothing to fit
to. Exactly what Setup E lacked — sample size (DEC-039).

The execution model does not favour us:

* entry and time exit are market, at the close of the last
  hourly bar, with slippage from the config;
* the stop is checked against the low/high of EVERY hourly bar between
  runs, not against the price at run time; on a gap through the stop
  the fill is at the bar's open, i.e. worse than the stop level;
* the taker fee on every fill.

The exchange state is stored in a file separate from the cycle's memory. This
matters: the cycle reconciles ITS memory with the exchange, and if the "exchange"
read the same table, there would be nothing to reconcile.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from src.backtest.engine import MarketBar
from src.execution.exchange import Fill, PaperExchange
from src.execution.plan import Order, OrderKind

HOUR = timedelta(hours=1)


class MarketPaperVenue(PaperExchange):
    """A paper exchange with persistent state and replay of hourly bars."""

    #: The moment up to which (exclusive) bars are replayed for ALL held
    #: symbols — the minimum: stop checks continue from it (P-001).
    replayed_until: datetime | None = None
    #: The latest moment the exchange has SEEN for at least one symbol —
    #: the maximum. It decides whether one can still decide "as of 00:10": one
    #: symbol without bars holds `replayed_until` in place, but the exchange has
    #: already seen other symbols' stops filled after the decision (fifth round
    #: of review 2026-09-28).
    seen_until: datetime | None = None
    #: The exchange has already been replayed PAST the decision moment by an earlier attempt
    #: the same day: the day's decision is made, a retry does not enter (otherwise an entry
    #: "as of 00:10" would see stops triggered after 00:10 — fourth review round).
    decision_passed: bool = False
    #: The decision moment for which the day's decision is MADE (the cycle reached
    #: entries). The day watch replays the exchange forward only after
    #: it: otherwise it would take from the night cycle the right to decide "as of 00:10".
    decided_for: datetime | None = None
    #: Where to save after every order (DEC-049). None — do not save
    #: (preview, tests).
    autosave: Path | None = None

    async def place(self, order: Order) -> None:
        try:
            await super().place(order)
        finally:
            if self.autosave is not None:
                self.save(self.autosave)

    async def cancel(self, link_id: str) -> None:
        await super().cancel(link_id)
        if self.autosave is not None:
            self.save(self.autosave)

    # -- price replay ---------------------------------------------------
    def replay(self, symbol: str, bars: list[MarketBar]) -> list[str]:
        """Replay of closed hourly bars through pending orders.

        The order of the high and low within a bar is unknown. For the stop
        it does not matter — a position has only one — and the setup has no target.
        Returns trigger events.
        """
        events: list[str] = []
        for bar in sorted(bars, key=lambda b: b.ts):
            held = self._positions.get(symbol)
            if held is not None:
                long, _qty, _entry = held
                stop = next(
                    (o for o in self._resting.values()
                     if o.symbol == symbol and o.kind is OrderKind.STOP_LOSS),
                    None,
                )
                if stop is not None and stop.price is not None:
                    level = stop.price
                    hit = bar.low <= level if long else bar.high >= level
                    if hit:
                        # Gap through the stop — fill at the open, worse than the level.
                        price = min(level, bar.open) if long else max(level, bar.open)
                        self.move_price(symbol, price)
                        events.append(
                            f"{symbol} stop {level} filled at {price} ({bar.ts:%Y-%m-%d %H:%M})"
                        )
            self.mark[symbol] = bar.close
            end = bar.ts + HOUR
            if self.seen_until is None or end > self.seen_until:
                self.seen_until = end
        return events

    # -- storage --------------------------------------------------------
    def dump(self) -> dict:
        def order(o: Order) -> dict:
            return {
                "kind": o.kind.value, "symbol": o.symbol, "buy": o.buy, "qty": str(o.qty),
                "price": None if o.price is None else str(o.price),
                "reduce_only": o.reduce_only, "link_id": o.link_id,
            }

        return {
            "replayed_until": self.replayed_until.isoformat() if self.replayed_until else None,
            "seen_until": self.seen_until.isoformat() if self.seen_until else None,
            "decided_for": self.decided_for.isoformat() if self.decided_for else None,
            "positions": {
                s: [long, str(qty), str(entry)]
                for s, (long, qty, entry) in self._positions.items()
            },
            "resting": [order(o) for o in self._resting.values()],
            "seen": sorted(self._seen),
            "fills": {
                k: [str(f.avg_price), str(f.qty), str(f.fee)] for k, f in self._fills.items()
            },
        }

    def restore(self, raw: dict) -> None:
        until = raw.get("replayed_until")
        self.replayed_until = datetime.fromisoformat(until) if until else None
        seen = raw.get("seen_until")
        self.seen_until = datetime.fromisoformat(seen) if seen else self.replayed_until
        decided = raw.get("decided_for")
        self.decided_for = datetime.fromisoformat(decided) if decided else None
        self._positions = {
            s: (bool(v[0]), Decimal(v[1]), Decimal(v[2]))
            for s, v in raw.get("positions", {}).items()
        }
        self._resting = {
            o["link_id"]: Order(
                kind=OrderKind(o["kind"]), symbol=o["symbol"], buy=o["buy"],
                qty=Decimal(o["qty"]),
                price=None if o["price"] is None else Decimal(o["price"]),
                reduce_only=o["reduce_only"], link_id=o["link_id"],
            )
            for o in raw.get("resting", [])
        }
        self._seen = set(raw.get("seen", []))
        self._fills = {
            k: Fill(Decimal(v[0]), Decimal(v[1]), Decimal(v[2]))
            for k, v in raw.get("fills", {}).items()
        }

    def save(self, path: Path) -> None:
        """Atomically: a half-written file would lose positions."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.dump(), ensure_ascii=False, indent=1))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path, **kwargs) -> MarketPaperVenue:  # noqa: ANN003
        venue = cls(**kwargs)
        if path.exists():
            venue.restore(json.loads(path.read_text()))
        return venue

    @property
    def held_symbols(self) -> list[str]:
        return sorted(self._positions)


__all__ = ["HOUR", "MarketPaperVenue"]
