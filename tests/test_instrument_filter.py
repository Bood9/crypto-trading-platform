"""Instrument Filter tests.

The numbers are taken from a real API response as of 2026-08-07, so the test catches
a regression against what the exchange actually returns, not against
made-up values.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import load_settings
from src.core.models import InstrumentSpec, RejectionCode
from src.exchange.instrument_filter import (
    assess,
    effective_min_notional,
    granularity_usd,
    is_crypto,
    max_parts,
    parse_instrument,
    parse_tier1_risk,
    round_qty_down,
)

D = Decimal


#: The deposit the expectations in this file were computed for. The tests check
#: the filter's arithmetic, not the current business value, so the input
#: is pinned explicitly: otherwise changing the deposit in the config (DEC-038) would silently
#: turn exact checks into checks of a different number.
DEPOSIT_UNDER_TEST = 40.0


@pytest.fixture
def settings():
    s = load_settings()
    return s.model_copy(
        update={"risk": s.risk.model_copy(update={"equity_usd": DEPOSIT_UNDER_TEST})}
    )


def spec(
    symbol: str = "SOLUSDT",
    *,
    base: str = "SOL",
    min_qty: str = "0.1",
    step: str = "0.1",
    min_notional: str = "5",
    status: str = "Trading",
    symbol_type: str = "",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        base_coin=base,
        quote_coin="USDT",
        contract_type="LinearPerpetual",
        status=status,
        tick_size=D("0.001"),
        qty_step=D(step),
        min_order_qty=D(min_qty),
        max_order_qty=D("1000000"),
        min_notional_value=D(min_notional),
        max_leverage=D("100"),
        funding_interval_minutes=480,
        symbol_type=symbol_type,
    )


CRYPTO_TYPES = {"", "innovation"}
EXCLUDED_COINS = {"XAU", "XAG", "XAUT", "XPT", "XPD", "CL", "BZ"}


# ---------------------------------------------------------------------------
# Effective minimum
# ---------------------------------------------------------------------------
def test_min_notional_binds_when_qty_is_cheap():
    """XRPUSDT: 1 XRP × $1.0353 is less than 5 USDT, so the notional is binding."""
    s = spec("XRPUSDT", base="XRP", min_qty="1", step="0.1")
    assert effective_min_notional(s, D("1.0353")) == D("5")


def test_min_order_qty_binds_for_expensive_coins():
    """BTCUSDT: minNotionalValue = 5, but 0.001 BTC × $65036 = $65.04.

    Exactly the case that made the spec wrongly assume that BTC has
    a minimum notional of 100 USDT.
    """
    s = spec("BTCUSDT", base="BTC", min_qty="0.001", step="0.001")
    assert effective_min_notional(s, D("65036.5")) == D("65.0365")


def test_effective_min_takes_the_stricter_of_the_two():
    s = spec(min_qty="0.1", step="0.1", min_notional="5")
    assert effective_min_notional(s, D("73.8")) == D("7.38")
    # A cheap coin: minNotionalValue is binding.
    assert effective_min_notional(s, D("10")) == D("5")


def test_zero_or_negative_price_is_an_error():
    s = spec()
    for bad in (D("0"), D("-1")):
        with pytest.raises(ValueError, match="must be positive"):
            effective_min_notional(s, bad)
        with pytest.raises(ValueError, match="must be positive"):
            granularity_usd(s, bad)


# ---------------------------------------------------------------------------
# Granularity and splitting
# ---------------------------------------------------------------------------
def test_granularity_is_step_times_price():
    s = spec("BTCUSDT", base="BTC", min_qty="0.001", step="0.001")
    assert granularity_usd(s, D("65036.5")) == D("65.0365")


def test_btc_position_cannot_be_split():
    """The lot step equals the whole position — partial exits are impossible."""
    s = spec("BTCUSDT", base="BTC", min_qty="0.001", step="0.001")
    assert max_parts(s, D("65036.5"), D("70")) == 1


def test_sol_position_splits_into_nine_parts():
    s = spec("SOLUSDT", min_qty="0.1", step="0.1")
    assert max_parts(s, D("73.8"), D("70")) == 9


def test_max_parts_is_zero_when_step_exceeds_position():
    s = spec("HUGEUSDT", base="HUGE", min_qty="1", step="1")
    assert max_parts(s, D("1000"), D("70")) == 0


# ---------------------------------------------------------------------------
# Quantity rounding
# ---------------------------------------------------------------------------
def test_qty_rounds_down_never_up():
    """Rounding up would increase the position beyond what the risk computed."""
    assert round_qty_down(D("0.9999"), D("0.1")) == D("0.9")
    assert round_qty_down(D("1.0"), D("0.1")) == D("1.0")
    assert round_qty_down(D("0.0009"), D("0.001")) == D("0")


def test_qty_rounding_is_exact_on_decimal_thirds():
    """With float 0.1*3 != 0.3, and the exchange would reject such a size."""
    assert round_qty_down(D("0.3"), D("0.1")) == D("0.3")
    assert round_qty_down(D("70") / D("73.8"), D("0.1")) == D("0.9")


def test_zero_step_is_rejected():
    with pytest.raises(ValueError):
        round_qty_down(D("1"), D("0"))


# ---------------------------------------------------------------------------
# DEC-002: crypto only
# ---------------------------------------------------------------------------
def test_plain_crypto_passes():
    assert is_crypto(spec("SOLUSDT", base="SOL"), CRYPTO_TYPES, EXCLUDED_COINS)
    assert is_crypto(spec("BTCUSDT", base="BTC"), CRYPTO_TYPES, EXCLUDED_COINS)


def test_innovation_zone_is_still_crypto():
    s = spec("AIOZUSDT", base="AIOZ", symbol_type="innovation")
    assert is_crypto(s, CRYPTO_TYPES, EXCLUDED_COINS)


@pytest.mark.parametrize(
    "symbol,base",
    [("AAPLUSDT", "AAPL"), ("SOXLUSDT", "SOXL"), ("MUUSDT", "MU"), ("SPCXUSDT", "SPCX")],
)
def test_tokenized_equities_are_excluded_by_symbol_type(symbol, base):
    """A manual per-coin stop list did not catch them: there are 174 stocks."""
    s = spec(symbol, base=base, symbol_type="stock")
    assert not is_crypto(s, CRYPTO_TYPES, EXCLUDED_COINS)


@pytest.mark.parametrize("symbol,base", [("XAUUSDT", "XAU"), ("CLUSDT", "CL")])
def test_commodities_are_excluded_by_symbol_type(symbol, base):
    s = spec(symbol, base=base, symbol_type="commodity")
    assert not is_crypto(s, CRYPTO_TYPES, EXCLUDED_COINS)


def test_base_coin_stoplist_catches_unlabelled_asset():
    """The second barrier: the exchange listed a metal but did not set symbolType."""
    s = spec("XAUUSDT", base="XAU", symbol_type="")
    assert not is_crypto(s, CRYPTO_TYPES, EXCLUDED_COINS)


def test_unknown_symbol_type_is_rejected_by_default():
    """A new asset class must not slip through silently."""
    s = spec("SOMETHINGUSDT", base="SMTH", symbol_type="prediction")
    assert not is_crypto(s, CRYPTO_TYPES, EXCLUDED_COINS)


# ---------------------------------------------------------------------------
# Full evaluation
# ---------------------------------------------------------------------------
def test_sol_is_tradable_with_partial_exits(settings):
    result = assess(spec("SOLUSDT"), D("73.8"), D("295000000"), settings)
    assert result.tradable
    assert result.supports_partial_exit
    assert result.rejection is None
    assert result.effective_min_notional == D("7.38")
    assert result.target_notional == D("70.00")


def test_btc_is_excluded_for_lack_of_partial_exit(settings):
    """Passes the minimum ($65.04 < $70), but the lot step equals the position.

    This is the real reason to exclude BTC — not "a minimum of 100 USDT" as
    the spec assumed. An instrument that cannot be exited in parts is
    a different instrument in terms of risk management.
    """
    s = spec("BTCUSDT", base="BTC", min_qty="0.001", step="0.001")
    result = assess(s, D("65036.5"), D("3564000000"), settings)

    assert not result.tradable
    assert result.rejection is RejectionCode.NO_PARTIAL_EXIT
    assert result.max_parts == 1
    # It did pass the minimum — it is rejected precisely for indivisibility.
    assert result.effective_min_notional < result.target_notional


def test_eth_passes_at_exactly_the_threshold(settings):
    """ETHUSDT splits into exactly 3 parts — the threshold boundary."""
    s = spec("ETHUSDT", base="ETH", min_qty="0.01", step="0.01")
    result = assess(s, D("1931.08"), D("1654000000"), settings)

    assert result.max_parts == 3
    assert result.tradable
    assert result.rejection is None


def test_two_parts_is_not_enough(settings):
    """The threshold is at least 3 parts; a ladder of two does not count."""
    s = spec("MIDUSDT", base="MID", min_qty="0.001", step="0.001")
    result = assess(s, D("30000"), D("100000000"), settings)

    assert result.max_parts == 2
    assert not result.tradable
    assert result.rejection is RejectionCode.NO_PARTIAL_EXIT


def test_symbol_above_target_notional_is_rejected(settings):
    """The price grew so much that the minimum lot costs more than the whole position."""
    s = spec("BTCUSDT", base="BTC", min_qty="0.001", step="0.001")
    result = assess(s, D("150000"), D("3564000000"), settings)

    assert not result.tradable
    assert result.rejection is RejectionCode.MIN_NOTIONAL
    assert result.effective_min_notional == D("150")


def test_tokenized_equity_is_rejected(settings):
    """SOXLUSDT: a semiconductor ETF, 5th by turnover on the exchange."""
    s = spec("SOXLUSDT", base="SOXL", symbol_type="stock")
    result = assess(s, D("143.46"), D("233000000"), settings)

    assert not result.tradable
    assert result.rejection is RejectionCode.NOT_CRYPTO
    assert result.note is not None
    assert "stock" in result.note


def test_commodity_is_rejected(settings):
    s = spec("XAUUSDT", base="XAU", symbol_type="commodity")
    result = assess(s, D("4320"), D("227000000"), settings)
    assert not result.tradable
    assert result.rejection is RejectionCode.NOT_CRYPTO


def test_non_trading_status_is_rejected(settings):
    result = assess(spec(status="PreLaunch"), D("73.8"), D("0"), settings)
    assert not result.tradable
    assert result.rejection is RejectionCode.NOT_TRADING


def test_coarse_step_is_rejected_even_when_minimum_passes(settings):
    """A coarse step: 70/45 rounds down to one lot = $45.

    It passes the minimum but splits into one part — rejected.
    """
    s = spec("ODDUSDT", base="ODD", min_qty="1", step="1", min_notional="5")
    result = assess(s, D("45"), D("1000000"), settings)

    assert result.max_parts == 1
    assert not result.tradable
    assert result.rejection is RejectionCode.NO_PARTIAL_EXIT


# ---------------------------------------------------------------------------
# Parsing API responses
# ---------------------------------------------------------------------------
def test_parse_instrument_reads_real_payload():
    """A real instruments-info response for BTCUSDT."""
    raw = {
        "symbol": "BTCUSDT",
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "launchTime": "1584230400000",
        "priceFilter": {"tickSize": "0.10"},
        "lotSizeFilter": {
            "minOrderQty": "0.001",
            "qtyStep": "0.001",
            "maxOrderQty": "1500.000",
            "minNotionalValue": "5",
        },
        "leverageFilter": {"maxLeverage": "100.00"},
        "fundingInterval": 480,
    }
    s = parse_instrument(raw)

    assert s.symbol == "BTCUSDT"
    assert s.min_order_qty == D("0.001")
    assert s.min_notional_value == D("5")
    assert s.funding_interval_minutes == 480
    assert s.launch_time is not None
    assert s.launch_time.year == 2020


def test_parse_instrument_tolerates_missing_fields():
    """For some symbols the fields arrive as empty strings."""
    raw = {
        "symbol": "XUSDT",
        "baseCoin": "X",
        "quoteCoin": "USDT",
        "status": "Trading",
        "contractType": "LinearPerpetual",
        "priceFilter": {"tickSize": ""},
        "lotSizeFilter": {"minOrderQty": "1", "qtyStep": "1", "maxOrderQty": "10"},
        "leverageFilter": {},
    }
    s = parse_instrument(raw)
    assert s.min_notional_value == D("0")
    assert s.tick_size == D("0")
    assert s.launch_time is None


def test_tier1_risk_picks_lowest_tier():
    """A real risk-limit response for SOLUSDT: MMR 0.5%, not 1%."""
    tiers = [
        {
            "riskLimitValue": "50000",
            "maintenanceMargin": "0.005",
            "initialMargin": "0.01",
            "isLowestRisk": 1,
        },
        {
            "riskLimitValue": "100000",
            "maintenanceMargin": "0.0056",
            "initialMargin": "0.0111",
            "isLowestRisk": 0,
        },
    ]
    mmr, im, limit = parse_tier1_risk(tiers)
    assert mmr == D("0.005")
    assert im == D("0.01")
    assert limit == D("50000")


def test_tier1_falls_back_to_smallest_limit_without_flag():
    tiers = [
        {"riskLimitValue": "100000", "maintenanceMargin": "0.0056", "isLowestRisk": 0},
        {"riskLimitValue": "50000", "maintenanceMargin": "0.005", "isLowestRisk": 0},
    ]
    mmr, _, limit = parse_tier1_risk(tiers)
    assert mmr == D("0.005")
    assert limit == D("50000")


def test_tier1_on_empty_input():
    assert parse_tier1_risk([]) == (None, None, None)
