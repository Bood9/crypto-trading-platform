"""Instrument Filter — which symbols are available to us at all with a $40 deposit.

The main finding of the 2026-08-07 reconnaissance: the constraint almost never
comes from `minNotionalValue` (it is 5 USDT for all checked pairs, BTC included).
It comes from `minOrderQty` multiplied by the price. For BTCUSDT that is
0.001 BTC × $65 036 ≈ $65 with a target notional of $70 — i.e. BTC formally
passes, but the lot step equals the whole position, so partial exits are
impossible. That is the real reason to exclude it.

Hence the rule: compute max(minNotionalValue, minOrderQty × price) and,
separately, the granularity qtyStep × price.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from src.core.config import Settings
from src.core.logging import get_logger
from src.core.models import InstrumentSpec, RejectionCode, Tradability
from src.exchange.bybit_client import BybitClient, BybitError

log = get_logger(__name__)

def _dec(value: object, default: str = "0") -> Decimal:
    """Bybit serves numbers as strings. Empty values are not an error."""
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


def parse_instrument(raw: dict, *, is_delisted: bool = False) -> InstrumentSpec:
    """Parses an item of /v5/market/instruments-info."""
    lot = raw.get("lotSizeFilter") or {}
    price = raw.get("priceFilter") or {}
    lev = raw.get("leverageFilter") or {}

    launch_raw = raw.get("launchTime")
    launch = None
    if launch_raw and str(launch_raw) != "0":
        launch = datetime.fromtimestamp(int(launch_raw) / 1000, tz=UTC)

    return InstrumentSpec(
        symbol=raw["symbol"],
        base_coin=raw.get("baseCoin", ""),
        quote_coin=raw.get("quoteCoin", ""),
        contract_type=raw.get("contractType", ""),
        status=raw.get("status", ""),
        tick_size=_dec(price.get("tickSize")),
        qty_step=_dec(lot.get("qtyStep")),
        min_order_qty=_dec(lot.get("minOrderQty")),
        max_order_qty=_dec(lot.get("maxOrderQty")),
        min_notional_value=_dec(lot.get("minNotionalValue")),
        max_leverage=_dec(lev.get("maxLeverage"), "1"),
        funding_interval_minutes=int(raw.get("fundingInterval") or 480),
        launch_time=launch,
        symbol_type=raw.get("symbolType") or "",
        is_delisted=is_delisted,
    )


def parse_tier1_risk(tiers: list[dict]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Extracts the first tier's MMR.

    At our position size we are always in the first tier, but the value is
    still pulled from the API: spec §16 forbids hardcoding the MMR, and it
    differs between symbols (0.5% for SOLUSDT, not 1% as assumed).
    """
    if not tiers:
        return None, None, None
    tier1 = next(
        (t for t in tiers if str(t.get("isLowestRisk")) in ("1", "True")),
        min(tiers, key=lambda t: _dec(t.get("riskLimitValue"), "1e18")),
    )
    return (
        _dec(tier1.get("maintenanceMargin")) or None,
        _dec(tier1.get("initialMargin")) or None,
        _dec(tier1.get("riskLimitValue")) or None,
    )


# ---------------------------------------------------------------------------
# Pure math. Covered by unit tests, spec §14.
# ---------------------------------------------------------------------------
def effective_min_notional(spec: InstrumentSpec, price: Decimal) -> Decimal:
    """The real minimum entry size in dollars.

    The exchange checks both constraints independently, so the stricter one
    binds.
    """
    if price <= 0:
        raise ValueError(f"{spec.symbol}: price must be positive, got {price}")
    return max(spec.min_notional_value, spec.min_order_qty * price)


def granularity_usd(spec: InstrumentSpec, price: Decimal) -> Decimal:
    """The minimum step of changing the position size, in dollars."""
    if price <= 0:
        raise ValueError(f"{spec.symbol}: price must be positive, got {price}")
    return spec.qty_step * price


def round_qty_down(qty: Decimal, step: Decimal) -> Decimal:
    """Rounds the quantity down to the lot step.

    Down only: rounding up would make the position larger than the one the
    Risk Manager computed, i.e. it would quietly breach the loss limit.
    """
    if step <= 0:
        raise ValueError("qty_step must be positive")
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def max_parts(spec: InstrumentSpec, price: Decimal, notional: Decimal) -> int:
    """Into how many parts a position of the given size divides.

    1 means a whole position: partial exits are impossible because any piece
    cut off is smaller than the lot step.
    """
    step_usd = granularity_usd(spec, price)
    if step_usd <= 0:
        return 0
    return int((notional / step_usd).to_integral_value(rounding=ROUND_DOWN))


def is_crypto(
    spec: InstrumentSpec,
    allowed_symbol_types: set[str],
    excluded_base_coins: set[str],
) -> bool:
    """DEC-002: crypto assets only.

    The asset class comes from `symbolType` — the only API field that tells
    tokenized stocks from crypto. Selecting by base coin does not work for
    this: there are already 174 stocks, and a manual list lags behind every
    new listing.

    The base coin stop list remains a second barrier in case the exchange
    introduces a new asset class and does not set `symbolType`.

    They are excluded because the logic of both setups is built on funding
    and forced-liquidation cascades of the leveraged crypto crowd, while the
    underlying markets of these instruments have weekends — gaps would break
    the ATR computation and exchange stops.
    """
    if spec.symbol_type not in allowed_symbol_types:
        return False
    return spec.base_coin.upper() not in excluded_base_coins


def assess(
    spec: InstrumentSpec,
    price: Decimal,
    turnover_24h: Decimal,
    settings: Settings,
) -> Tradability:
    """Full tradability check of one symbol at our deposit."""
    risk = settings.risk
    allowed_types = set(settings.universe.allowed_symbol_types)
    excluded = {c.upper() for c in settings.universe.excluded_base_coins}

    margin = Decimal(str(risk.equity_usd)) * Decimal(str(risk.margin_per_trade_pct)) / 100
    target_notional = margin * Decimal(str(risk.leverage_cap))

    def reject(code: RejectionCode, note: str) -> Tradability:
        return Tradability(
            symbol=spec.symbol,
            price=price,
            turnover_24h_usd=turnover_24h,
            effective_min_notional=Decimal(0),
            granularity_usd=Decimal(0),
            max_parts=0,
            target_notional=target_notional,
            tradable=False,
            rejection=code,
            note=note,
            mmr=spec.mmr,
        )

    if not spec.is_trading and not spec.is_delisted:
        return reject(RejectionCode.NOT_TRADING, f"status {spec.status!r}")
    if settings.universe.crypto_only and not is_crypto(spec, allowed_types, excluded):
        return reject(
            RejectionCode.NOT_CRYPTO,
            f"symbolType={spec.symbol_type!r}, base coin {spec.base_coin}",
        )
    if price <= 0:
        return reject(RejectionCode.EXCHANGE_ERROR, "price not received")

    eff_min = effective_min_notional(spec, price)
    step_usd = granularity_usd(spec, price)
    parts = max_parts(spec, price, target_notional)

    common = {
        "symbol": spec.symbol,
        "price": price,
        "turnover_24h_usd": turnover_24h,
        "effective_min_notional": eff_min,
        "granularity_usd": step_usd,
        "max_parts": parts,
        "target_notional": target_notional,
        "mmr": spec.mmr,
    }

    if eff_min > target_notional:
        return Tradability(
            **common,
            tradable=False,
            rejection=RejectionCode.MIN_NOTIONAL,
            note=f"minimum ${eff_min:.2f} > target notional ${target_notional:.2f}",
        )

    # Rounding down must not take the size below the exchange minimum.
    qty = round_qty_down(target_notional / price, spec.qty_step)
    if qty <= 0 or qty * price < eff_min:
        return Tradability(
            **common,
            tradable=False,
            rejection=RejectionCode.QTY_GRANULARITY,
            note=f"after rounding to step {spec.qty_step} the size is below the minimum",
        )

    # An instrument that cannot be divided is a different instrument in terms
    # of risk management: no taking part off at 1R, no laddered exit.
    # Marking it with an asterisk and leaving it in the universe is not an
    # option — sooner or later the logic will pick it up.
    min_parts = settings.universe.min_position_parts
    if parts < min_parts:
        return Tradability(
            **common,
            tradable=False,
            rejection=RejectionCode.NO_PARTIAL_EXIT,
            note=(
                f"divides into {parts} part(s) with a threshold of {min_parts}: "
                f"step ${step_usd:.2f} versus notional ${target_notional:.2f}"
            ),
        )

    return Tradability(**common, tradable=True, rejection=None, note=None)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
class InstrumentFilter:
    """Collects instrument parameters and builds the tradability report."""

    def __init__(self, client: BybitClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def fetch_specs(self) -> dict[str, InstrumentSpec]:
        raw = await self._client.get_instruments()
        return {r["symbol"]: parse_instrument(r) for r in raw}

    async def fetch_prices(self) -> dict[str, tuple[Decimal, Decimal]]:
        """Price and 24h turnover per symbol."""
        tickers = await self._client.get_tickers()
        return {
            t["symbol"]: (_dec(t.get("lastPrice")), _dec(t.get("turnover24h")))
            for t in tickers
        }

    async def enrich_mmr(
        self, specs: dict[str, InstrumentSpec], symbols: list[str], *, concurrency: int = 8
    ) -> dict[str, InstrumentSpec]:
        """Pulls the first tier's MMR for a list of symbols.

        Requested only for selected candidates: it is a separate request per
        symbol, and pulling it for all 800 instruments would burn the limit
        for data that will not be needed.
        """
        sem = asyncio.Semaphore(concurrency)
        updated = dict(specs)

        async def one(symbol: str) -> None:
            async with sem:
                try:
                    tiers = await self._client.get_risk_limit(symbol)
                except BybitError as exc:
                    log.warning("risk_limit.failed", symbol=symbol, error=str(exc))
                    return
            mmr, im, limit = parse_tier1_risk(tiers)
            if mmr is None:
                return
            updated[symbol] = specs[symbol].model_copy(
                update={
                    "mmr": mmr,
                    "initial_margin_rate": im,
                    "risk_limit_value": limit,
                }
            )

        await asyncio.gather(*(one(s) for s in symbols if s in specs))
        return updated

    async def build_report(
        self, *, enrich_top: int | None = None
    ) -> tuple[list[Tradability], dict[str, InstrumentSpec]]:
        """Tradability report sorted by turnover.

        MMR is pulled only for the top by turnover — the rest do not need it,
        because they never reach the position size computation.
        """
        specs, prices = await asyncio.gather(self.fetch_specs(), self.fetch_prices())

        universe = self._settings.universe
        allowed_types = set(universe.allowed_symbol_types)
        excluded = {c.upper() for c in universe.excluded_base_coins}

        candidates = [
            s
            for s in specs.values()
            if s.quote_coin == universe.quote_coin
            and s.contract_type == universe.contract_type
            and s.is_trading
            and (not universe.crypto_only or is_crypto(s, allowed_types, excluded))
        ]
        def turnover(spec: InstrumentSpec) -> Decimal:
            return prices.get(spec.symbol, (Decimal(0), Decimal(0)))[1]

        candidates.sort(key=turnover, reverse=True)

        top_n = enrich_top if enrich_top is not None else universe.top_n * 2
        specs = await self.enrich_mmr(specs, [s.symbol for s in candidates[:top_n]])

        report = [
            assess(specs[s.symbol], *prices.get(s.symbol, (Decimal(0), Decimal(0))), self._settings)
            for s in candidates
        ]
        report.sort(key=lambda t: t.turnover_24h_usd, reverse=True)
        return report, specs
