"""Funding accrual for holding a position. Spec §3.

`Trade.funding_paid` existed from the very beginning and always stayed
zero: the backtest held positions for free. The fourth case in a row where
a field exists but the value is not computed.

The sign is easy to mix up here, and the error looks harmless: the value
is small, while the direction flips its contribution. So both
sides are checked separately.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtest.engine import (
    BacktestConfig,
    FundingSchedule,
    MarketBar,
    Signal,
    run,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H8 = timedelta(hours=8)


def schedule(*rates: str, start: datetime = T0 + H8) -> FundingSchedule:
    return FundingSchedule(
        ts=[start + H8 * i for i in range(len(rates))],
        rates=[D(r) for r in rates],
    )


def bars(n: int = 12) -> list[MarketBar]:
    return [
        MarketBar(T0 + timedelta(hours=4) * i, D("100"), D("101"), D("99"), D("100"))
        for i in range(n)
    ]


def signal_at_third(is_long: bool = True):
    def fn(window):
        if len(window) != 3:
            return None
        if is_long:
            return Signal(is_long=True, entry=D("100"), stop=D("90"), target=D("120"))
        return Signal(is_long=False, entry=D("100"), stop=D("110"), target=D("80"))

    return fn


def sizer(_signal, _price):
    return D("2"), D("200"), D("20")


CONFIG = BacktestConfig(
    initial_equity=D("1000"),
    taker_fee_pct=D("0"),
    slippage_pct=D("0"),
    time_stop=timedelta(hours=20),
    warmup_bars=2,
)


# ---------------------------------------------------------------------------
# Accrual window
# ---------------------------------------------------------------------------
def test_charge_at_the_moment_of_entry_belongs_to_the_previous_holder():
    """An accrual exactly at the moment of entry is charged to whoever held
    the position before it, not to us."""
    s = FundingSchedule(ts=[T0, T0 + H8], rates=[D("0.01"), D("0.02")])
    assert s.between(T0, T0 + H8) == D("0.02")


def test_charge_at_the_moment_of_exit_is_ours():
    s = FundingSchedule(ts=[T0 + H8], rates=[D("0.02")])
    assert s.between(T0, T0 + H8) == D("0.02")
    assert s.between(T0, T0 + H8 - timedelta(seconds=1)) == D("0")


def test_empty_schedule_charges_nothing():
    assert FundingSchedule([], []).between(T0, T0 + H8 * 10) == D("0")


# ---------------------------------------------------------------------------
# Sign
# ---------------------------------------------------------------------------
def test_long_pays_when_funding_is_positive():
    result = run(
        bars(), signal_at_third(True), sizer, CONFIG,
        funding=schedule("0.001", "0.001"),
    )
    assert result.outcomes
    assert result.outcomes[0].trade.funding_paid > 0


def test_short_receives_when_funding_is_positive():
    result = run(
        bars(), signal_at_third(False), sizer, CONFIG,
        funding=schedule("0.001", "0.001"),
    )
    assert result.outcomes[0].trade.funding_paid < 0


def test_long_receives_when_funding_is_negative():
    result = run(
        bars(), signal_at_third(True), sizer, CONFIG,
        funding=schedule("-0.001", "-0.001"),
    )
    assert result.outcomes[0].trade.funding_paid < 0


def test_without_a_schedule_holding_is_free():
    """Exactly the behaviour there was until now — and it is
    in our favour."""
    result = run(bars(), signal_at_third(True), sizer, CONFIG)
    assert result.outcomes[0].trade.funding_paid == D("0")


# ---------------------------------------------------------------------------
# Effect on the result
# ---------------------------------------------------------------------------
def test_funding_reduces_the_r_multiple_of_a_long():
    free = run(bars(), signal_at_third(True), sizer, CONFIG)
    paid = run(
        bars(), signal_at_third(True), sizer, CONFIG,
        funding=schedule("0.01", "0.01"),
    )
    assert paid.outcomes[0].r_multiple < free.outcomes[0].r_multiple


def test_charge_scales_with_position_size():
    def double(_signal, _price):
        return D("4"), D("400"), D("40")

    single = run(
        bars(), signal_at_third(True), sizer, CONFIG, funding=schedule("0.01")
    )
    twice = run(
        bars(), signal_at_third(True), double, CONFIG, funding=schedule("0.01")
    )
    assert twice.outcomes[0].trade.funding_paid == (
        single.outcomes[0].trade.funding_paid * 2
    )


def test_r_multiple_is_unchanged_by_size_even_with_funding():
    """R must not depend on size: both fees and funding grow
    with the position. The portfolio recalculation rests on this property."""
    def double(_signal, _price):
        return D("4"), D("400"), D("40")

    single = run(
        bars(), signal_at_third(True), sizer, CONFIG, funding=schedule("0.01")
    )
    twice = run(
        bars(), signal_at_third(True), double, CONFIG, funding=schedule("0.01")
    )
    assert single.outcomes[0].r_multiple == twice.outcomes[0].r_multiple
