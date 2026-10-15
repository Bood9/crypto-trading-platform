"""Bybit testnet connection check before the first cycle. DEC-041.

Steps go from safe to acting, and the first failure stops
the rest: there is no point placing an order if the clock disagrees with the exchange or
the key has the right to withdraw funds.

The order round (`roundtrip`) is the minimum size on test money
with the same Executor that runs in the cycle: entry, stop on the exchange, reconciliation,
time close, stop cancellation, fills. This is the only way
to make sure the exchange responses match how the adapter understood them:
the format is known only from the documentation (DEC-041).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.backtest.engine import Signal
from src.exchange.trading_client import BybitTradingClient, TradingAPIError
from src.execution.bybit_exchange import BybitExchange
from src.execution.executor import close_by_time, open_position
from src.execution.plan import OrderPlan, PlanRejected, build, time_close

#: More than this — signed requests start being rejected (5 s window).
MAX_CLOCK_SKEW_S = 2.0


@dataclass
class Step:
    name: str
    ok: bool
    detail: str
    warn: bool = False


@dataclass
class CheckReport:
    steps: list[Step] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str, *, warn: bool = False) -> bool:
        self.steps.append(Step(name, ok, detail, warn))
        return ok


def permissions_problem(info: dict[str, Any]) -> str | None:
    """What is wrong with the key permissions, or None.

    Withdrawal is always forbidden: a trading bot key with withdrawal rights
    is a key that can be used to take the money away. Read-only is also a rejection:
    with it the bot cannot place a stop.
    """
    perms = info.get("permissions") or {}
    wallet = [p for p in perms.get("Wallet", []) if p]
    if any("Withdraw" in p for p in wallet) or "Withdraw" in str(perms.get("Withdraw", "")):
        return "the key has the WITHDRAWAL permission — create a key without it"
    if str(info.get("readOnly")) == "1":
        return "the key is read-only — trading is needed (Contract/Derivatives: Orders, Positions)"
    return None


async def run_check(
    client: BybitTradingClient,
    *,
    roundtrip_symbol: str | None = None,
    now: datetime | None = None,
) -> CheckReport:
    report = CheckReport()
    venue = BybitExchange(client)

    # 1. Clock.
    started = time.time()
    server = await client.get("/v5/market/time", {})
    local = (started + time.time()) / 2
    skew = abs(int(server.get("timeSecond", 0)) - local)
    if not report.add("clock", skew <= MAX_CLOCK_SKEW_S, f"skew vs the exchange {skew:.1f} s"):
        return report

    # 2. Key and balance.
    try:
        wallet = await client.get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    except TradingAPIError as exc:
        report.add("key", False, f"the exchange rejected the key: {exc}")
        return report
    usdt = Decimal(0)
    for account in wallet.get("list") or []:
        for coin in account.get("coin") or []:
            if coin.get("coin") == "USDT":
                usdt = Decimal(str(coin.get("walletBalance") or "0"))
    if not report.add("key and balance", True, f"key accepted, USDT on testnet: {usdt:.2f}"):
        return report
    if usdt < 50:
        report.add("balance", False, "under 50 USDT — top up the test account at testnet.bybit.com")
        return report

    # 3. Key permissions.
    info = await client.get("/v5/user/query-api", {})
    problem = permissions_problem(info)
    ips = info.get("ips") or []
    bound = bool(ips) and ips != ["*"]
    fine = f"trading yes, withdrawal no; IP binding: {'yes' if bound else 'no'}"
    if not report.add("key permissions", problem is None, problem or fine):
        return report

    # 4. Clean account.
    positions = await venue.positions()
    orders = await venue.open_orders()
    clean = not positions and not orders
    report.add(
        "clean account", clean,
        "no positions or orders" if clean else
        f"the account has {len(positions)} positions and {len(orders)} orders — reconciliation "
        "will treat them as foreign and disable trading. Close them or use a separate subaccount",
    )
    if not clean:
        return report

    # 5. Position mode: our orders are one-way mode (positionIdx 0).
    probe = roundtrip_symbol or "BTCUSDT"
    rows = (await client.get(
        "/v5/position/list", {"category": client.category, "symbol": probe}
    )).get("list") or []
    hedge = any(int(r.get("positionIdx", 0)) in (1, 2) for r in rows)
    if not report.add("position mode", not hedge,
                      "one-way" if not hedge else
                      "hedge mode — switch to One-Way in the testnet derivatives settings"):
        return report

    if roundtrip_symbol is None:
        return report
    await roundtrip(venue, roundtrip_symbol, report, now or datetime.now(UTC))
    return report


def min_qty_for(spec, price: Decimal) -> Decimal:  # noqa: ANN001
    """The smallest size that passes both the minimum lot and the exchange's minimum
    notional — with a 10% margin for price movement."""
    need = max(spec.min_order_qty, spec.min_notional * Decimal("1.1") / price)
    steps = math.ceil(need / spec.qty_step)
    return spec.qty_step * steps


async def roundtrip(venue: BybitExchange, symbol: str, report: CheckReport, now: datetime) -> None:
    spec = await venue.instrument(symbol)
    price = await venue.last_price(symbol)
    if not report.add("instrument", spec is not None and price is not None,
                      f"{symbol}: price {price}, price tick {spec.tick_size if spec else '—'}, "
                      f"min lot {spec.min_order_qty if spec else '—'}"):
        return
    qty = min_qty_for(spec, price)
    plan = build(
        Signal(is_long=True, entry=price, stop=price * Decimal("0.9"), target=None),
        spec=spec, qty=qty, moment=now,
    )
    if isinstance(plan, PlanRejected):
        report.add("plan", False, plan.note)
        return
    assert isinstance(plan, OrderPlan)

    opened = await open_position(venue, plan, moment=now)
    ok = opened.entered and opened.protected and not opened.halt
    detail = (f"long {plan.qty} {symbol}, stop {plan.stop_price} on the exchange"
              if ok else "; ".join(opened.notes) or (
                  opened.reconciliation.describe() if opened.reconciliation else "no details"))
    report.add("entry and stop", ok, detail)
    entry = await venue.fill(plan.entry.link_id, symbol)
    report.add("entry fill", entry is not None,
               f"at {entry.avg_price}, fee {entry.fee}" if entry else
               "order history did not return the entry fill", warn=entry is None)

    # Close in any case — even if the entry "was not confirmed":
    # the position may have opened, and it must not be left on the account.
    closing = await close_by_time(venue, plan, moment=now)
    report.add("close", closing.closed and closing.stop_cancelled,
               "position closed, stop cancelled" if closing.closed and closing.stop_cancelled
               else "; ".join(closing.notes) or "the stop remained on the exchange")
    exit_fill = await venue.fill(time_close(plan, now).link_id, symbol)
    if entry and exit_fill:
        pnl = (exit_fill.avg_price - entry.avg_price) * entry.qty - entry.fee - exit_fill.fee
        report.add("accounting", True, f"exit at {exit_fill.avg_price}, round {pnl:+.4f} USDT")
    else:
        report.add("accounting", False, "exit fill not found in the order history", warn=True)
    left = await venue.positions()
    report.add("account after the round", not left and not await venue.open_orders(),
               "clean" if not left else f"positions left: {len(left)} — close them manually")


__all__ = ["CheckReport", "Step", "min_qty_for", "permissions_problem", "run_check"]
