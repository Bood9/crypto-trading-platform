"""WebSocket collector: liquidations and order book snapshots.

Both datasets are research-only. They do NOT take part in trading logic,
setup definitions or the backtest.

Liquidations (DEC-001): the Setup A trigger is defined via OI contraction
and stays that way. Replacing it with real liquidations would drive the
backtest apart from live trading and require a full re-backtest from
scratch. The dataset is needed to measure, in half a year, whether the OI
trigger correlates with actual cascades.

Order book (DEC-007): the spread does not filter the universe. Snapshots
are needed to get the real spread distribution and to check the
Abdi–Ranaldo estimate against reality.

Since the connection is opened for the first anyway, the second comes
almost for free.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

import websockets

from src.core.bulk import upsert_chunked
from src.core.db import Liquidation, OrderbookSnapshot
from src.core.logging import get_logger

log = get_logger(__name__)

PUBLIC_WS_LINEAR = "wss://stream.bybit.com/v5/public/linear"

#: Order book subscription depth. Bybit serves 1, 50, 200 or 500 levels;
#: 50 is plenty for the 5 and 20 aggregates and is cheaper than the larger ones.
ORDERBOOK_DEPTH = 50

#: Bybit drops the connection if service messages are not answered.
PING_INTERVAL_S = 20.0

#: Maximum topics in one subscription. More — the exchange rejects the request.
MAX_TOPICS_PER_REQUEST = 10


def _dec(value: object) -> Decimal:
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def parse_liquidation(topic_data: dict, *, symbol: str) -> dict | None:
    """Parses a liquidation event.

    Returns None on an unrecognized message shape: P-001, better to lose a
    record of the research dataset than to write garbage that will later be
    taken for fact.
    """
    try:
        ts = datetime.fromtimestamp(int(topic_data["T"]) / 1000, tz=UTC)
        return {
            "ts": ts,
            "symbol": topic_data.get("s") or symbol,
            # On Bybit the side is the side of the CLOSING order, i.e. the
            # opposite of the liquidated position's side. Stored as received;
            # interpretation is the dataset consumer's concern.
            "side": str(topic_data.get("S", ""))[:8],
            "qty": _dec(topic_data.get("v") or topic_data.get("size")),
            "price": _dec(topic_data.get("p") or topic_data.get("price")),
        }
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("ws.liquidation.malformed", symbol=symbol, error=str(exc))
        return None


def _depth_usd(levels: Sequence[Sequence[str]], count: int) -> Decimal:
    """Total value of the first `count` levels in dollars."""
    total = Decimal(0)
    for level in levels[:count]:
        if len(level) < 2:
            continue
        total += _dec(level[0]) * _dec(level[1])
    return total


def build_snapshot(symbol: str, book: dict, ts: datetime) -> dict | None:
    """Builds a snapshot row from the order book state.

    Returns None if the book is one-sided or prices are zero: the spread is
    undefined in that state, and substituting zero is not allowed — it would
    read as "no spread".
    """
    bids = book.get("b") or []
    asks = book.get("a") or []
    if not bids or not asks:
        return None

    best_bid = _dec(bids[0][0])
    best_ask = _dec(asks[0][0])
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None

    mid = (best_bid + best_ask) / 2
    if mid <= 0:
        return None

    return {
        "symbol": symbol,
        "ts": ts,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_qty": _dec(bids[0][1]),
        "best_ask_qty": _dec(asks[0][1]),
        "spread_pct": (best_ask - best_bid) / mid * 100,
        "bid_depth_5_usd": _depth_usd(bids, 5),
        "ask_depth_5_usd": _depth_usd(asks, 5),
        "bid_depth_20_usd": _depth_usd(bids, 20),
        "ask_depth_20_usd": _depth_usd(asks, 20),
    }


def apply_orderbook_delta(book: dict, data: dict, msg_type: str) -> dict:
    """Applies a snapshot or delta to the local order book state.

    Bybit sends a full snapshot on subscription and after a disconnect, then
    only changes. A level with zero size means deletion.
    """
    if msg_type == "snapshot" or not book:
        return {"b": list(data.get("b") or []), "a": list(data.get("a") or [])}

    for side in ("b", "a"):
        updates = data.get(side) or []
        if not updates:
            continue
        levels = {price: size for price, size in (lvl[:2] for lvl in book.get(side, []))}
        for lvl in updates:
            if len(lvl) < 2:
                continue
            price, size = lvl[0], lvl[1]
            if _dec(size) == 0:
                levels.pop(price, None)
            else:
                levels[price] = size
        # Bids descending, asks ascending — otherwise the "best" level
        # would not be first.
        ordered = sorted(levels.items(), key=lambda kv: Decimal(kv[0]), reverse=(side == "b"))
        book[side] = [[p, s] for p, s in ordered]

    return book


async def store_liquidations(rows: list[dict]) -> int:
    # Surrogate key, conflicts do not happen — nothing to update.
    return await upsert_chunked(
        Liquidation, rows, index_elements=[Liquidation.id, Liquidation.ts]
    )


async def store_snapshots(rows: list[dict]) -> int:
    # Idempotency: a repeated snapshot of the same minute overwrites the
    # previous one instead of failing on a duplicate key.
    return await upsert_chunked(
        OrderbookSnapshot,
        rows,
        index_elements=[OrderbookSnapshot.symbol, OrderbookSnapshot.ts],
        update_columns=(
            "best_bid",
            "best_ask",
            "best_bid_qty",
            "best_ask_qty",
            "spread_pct",
            "bid_depth_5_usd",
            "ask_depth_5_usd",
            "bid_depth_20_usd",
            "ask_depth_20_usd",
        ),
    )


class WebSocketCollector:
    """One connection for both streams.

    Order book snapshots are written on a schedule (once per
    `snapshot_interval_s`), not on every update: the delta stream runs at
    dozens of messages per second per symbol, and there is no point storing
    all of it — we care about the distribution, not the microstructure.
    """

    def __init__(
        self,
        symbols: Iterable[str],
        *,
        url: str = PUBLIC_WS_LINEAR,
        snapshot_interval_s: float = 60.0,
        flush_interval_s: float = 10.0,
        collect_orderbook: bool = True,
    ) -> None:
        self._symbols = list(symbols)
        self._url = url
        self._snapshot_interval = snapshot_interval_s
        self._flush_interval = flush_interval_s
        self._collect_orderbook = collect_orderbook

        self._books: dict[str, dict] = {}
        self._pending_liquidations: list[dict] = []
        self._pending_snapshots: list[dict] = []
        self._last_snapshot_at: dict[str, datetime] = {}
        self._running = False

    def topics(self) -> list[str]:
        topics = [f"allLiquidation.{s}" for s in self._symbols]
        if self._collect_orderbook:
            topics += [f"orderbook.{ORDERBOOK_DEPTH}.{s}" for s in self._symbols]
        return topics

    def handle_message(self, message: dict, now: datetime | None = None) -> None:
        """Handles one message. Split out for testability."""
        moment = now or datetime.now(UTC)
        topic = message.get("topic") or ""

        if topic.startswith("allLiquidation."):
            symbol = topic.split(".", 1)[1]
            payload = message.get("data") or []
            # Bybit sends both a list and a single object.
            events = payload if isinstance(payload, list) else [payload]
            for event in events:
                row = parse_liquidation(event, symbol=symbol)
                if row is not None:
                    self._pending_liquidations.append(row)
            return

        if topic.startswith("orderbook."):
            symbol = topic.rsplit(".", 1)[-1]
            data = message.get("data") or {}
            book = apply_orderbook_delta(
                self._books.get(symbol, {}), data, message.get("type", "delta")
            )
            self._books[symbol] = book

            last = self._last_snapshot_at.get(symbol)
            due = last is None or (moment - last).total_seconds() >= self._snapshot_interval
            if due:
                snapshot = build_snapshot(symbol, book, moment)
                if snapshot is not None:
                    self._pending_snapshots.append(snapshot)
                    self._last_snapshot_at[symbol] = moment

    async def _flush(self) -> tuple[int, int]:
        liquidations, self._pending_liquidations = self._pending_liquidations, []
        snapshots, self._pending_snapshots = self._pending_snapshots, []
        written_l = await store_liquidations(liquidations)
        written_s = await store_snapshots(snapshots)
        if written_l or written_s:
            log.info("ws.flushed", liquidations=written_l, snapshots=written_s)
        return written_l, written_s

    async def _flush_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._flush_interval)
            try:
                await self._flush()
            except Exception as exc:  # a write must not bring down collection
                log.error("ws.flush_failed", error=str(exc))

    async def run(self, *, max_reconnects: int | None = None) -> None:
        """Holds the connection, reconnecting on disconnects.

        A disconnect is a normal event, not a failure: the exchange
        periodically drops long connections itself. Resubscribe and continue.
        """
        self._running = True
        flusher = asyncio.create_task(self._flush_loop())
        attempt = 0

        try:
            while self._running:
                try:
                    await self._session()
                    attempt = 0
                except (OSError, websockets.WebSocketException) as exc:
                    attempt += 1
                    if max_reconnects is not None and attempt > max_reconnects:
                        raise
                    # Jitter is mandatory: without it all collectors rush back
                    # at the same time after a common disconnect.
                    delay = min(2**attempt, 60) * (0.5 + random.random() / 2)
                    log.warning(
                        "ws.reconnect",
                        attempt=attempt,
                        delay_s=round(delay, 1),
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
        finally:
            self._running = False
            flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await flusher
            await self._flush()

    async def _session(self) -> None:
        async with websockets.connect(self._url, ping_interval=PING_INTERVAL_S) as ws:
            topics = self.topics()
            for i in range(0, len(topics), MAX_TOPICS_PER_REQUEST):
                chunk = topics[i : i + MAX_TOPICS_PER_REQUEST]
                await ws.send(json.dumps({"op": "subscribe", "args": chunk}))

            log.info("ws.connected", topics=len(topics), symbols=len(self._symbols))

            async for raw in ws:
                message = json.loads(raw)
                if message.get("op") in ("subscribe", "pong"):
                    continue
                self.handle_message(message)

    def stop(self) -> None:
        self._running = False
