"""Moving the stop to breakeven. Spec §6, strategy.management.

The rule was in the config from the very beginning and implemented
nowhere: `CloseReason.BREAKEVEN` existed, the logic did not.

Conservatism is checked first of all. Moving the stop is a rare
case where an error in favour of the result looks harmless: the trade
merely "closed at zero instead of a loss".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtest.accounting import CloseReason
from src.backtest.engine import (
    BacktestConfig,
    MarketBar,
    Signal,
    breakeven_level,
    run,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=4)


def bar(i: int, high: str, low: str, close: str = "100") -> MarketBar:
    return MarketBar(T0 + H * i, D("100"), D(high), D(low), D(close), D("1"))


def series(*action: MarketBar) -> list[MarketBar]:
    """Three calm bars, then the given ones.

    The signal fires on the third (`len(window) == 3`), the position
    opens on it as well, and exits are checked from the NEXT one —
    so the action starts from the fourth bar.
    """
    quiet = [bar(0, "101", "99"), bar(1, "101", "99"), bar(2, "101", "99")]
    tail = [
        bar(len(quiet) + len(action) + i, "101", "99") for i in range(3)
    ]
    shifted = [
        MarketBar(T0 + H * (len(quiet) + i), b.open, b.high, b.low, b.close, b.volume)
        for i, b in enumerate(action)
    ]
    return quiet + shifted + tail


def config(*, breakeven: str | None = "1.0", fee: str = "0.055") -> BacktestConfig:
    return BacktestConfig(
        initial_equity=D("1000"),
        taker_fee_pct=D(fee),
        slippage_pct=D("0"),
        time_stop=timedelta(hours=400),
        warmup_bars=2,
        breakeven_at_r=D(breakeven) if breakeven is not None else None,
    )


def long_once(entry="100", stop="90", target="130"):
    def fn(window):
        if len(window) != 3:
            return None
        return Signal(is_long=True, entry=D(entry), stop=D(stop), target=D(target))

    return fn


def sizer(_signal, _price):
    return D("1"), D("100"), D("10")


# ---------------------------------------------------------------------------
# Level
# ---------------------------------------------------------------------------
def test_level_covers_the_round_trip_commission():
    """A stop exactly at the entry price would leave the round-trip fee
    unpaid, i.e. it would turn a zero trade into a negative one."""
    long = breakeven_level(D("100"), is_long=True, fee_pct=D("0.055"))
    short = breakeven_level(D("100"), is_long=False, fee_pct=D("0.055"))

    assert long == D("100") * (1 + D("0.0011"))
    assert short == D("100") * (1 - D("0.0011"))
    assert long > D("100") > short


def test_zero_fee_puts_the_level_at_the_entry():
    assert breakeven_level(D("100"), is_long=True, fee_pct=D("0")) == D("100")


# ---------------------------------------------------------------------------
# Conservatism
# ---------------------------------------------------------------------------
def test_the_moved_stop_takes_effect_only_from_the_next_bar():
    """Within a single bar it is unknown what happened first —
    reaching the threshold or returning to the entry price. Assuming the threshold
    came first means passing losing trades off as breakeven ones.
    """
    # The bar reaches both +1R (110) and the original stop (90).
    bars = series(bar(0, "115", "85"))

    result = run(bars, long_once(), sizer, config())
    assert result.outcomes
    trade = result.outcomes[0].trade
    assert trade.close_reason is CloseReason.STOP
    assert trade.exit_price == D("90")


def test_stop_moves_after_the_threshold_bar_and_saves_the_trade():
    bars = series(
        bar(0, "112", "99"),   # +1R threshold reached
        bar(1, "101", "85"),   # return below entry
    )

    result = run(bars, long_once(), sizer, config())
    trade = result.outcomes[0].trade
    assert trade.close_reason is CloseReason.BREAKEVEN
    assert trade.exit_price == breakeven_level(
        D("100"), is_long=True, fee_pct=D("0.055")
    )
    # The R denominator stays the original one: the move does not rewrite the risk
    # we took at the moment of entry.
    assert trade.stop == D("90")
    assert trade.risk_per_unit == D("10")


def test_result_is_a_small_loss_not_a_clean_zero():
    """Slippage works against us here too. "Breakeven"
    covers the fee, not everything at once."""
    bars = series(bar(0, "112", "99"), bar(1, "101", "85"))

    cfg = config()
    cfg.slippage_pct = D("0.05")
    result = run(bars, long_once(), sizer, cfg)
    r = result.outcomes[0].r_multiple
    assert r is not None
    assert D("-0.05") < r < D("0")


def test_without_slippage_the_trade_closes_at_about_zero():
    bars = series(bar(0, "112", "99"), bar(1, "101", "85"))

    result = run(bars, long_once(), sizer, config())
    r = result.outcomes[0].r_multiple
    assert abs(r) < D("0.001")


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------
def test_disabled_by_default():
    """Enabling it changes the result of all past runs, and Setup B
    was rejected with specific numbers."""
    assert BacktestConfig(
        initial_equity=D("1000"), taker_fee_pct=D("0"), slippage_pct=D("0"),
        time_stop=timedelta(hours=1),
    ).breakeven_at_r is None

    bars = series(bar(0, "112", "99"), bar(1, "101", "85"))

    result = run(bars, long_once(), sizer, config(breakeven=None))
    assert result.outcomes[0].trade.close_reason is CloseReason.STOP


def test_stop_never_moves_against_us():
    """Defensive branch: the move is allowed only in OUR direction.

    Unreachable with a correct signal — for a long, breakeven is always above
    the entry and the original stop is always below. Checked directly because
    the branch guards against a degenerate signal, not against a market
    situation.
    """
    from src.backtest.engine import OpenPosition, _arm_breakeven

    # Degenerate signal: the stop is ABOVE the entry for a long.
    position = OpenPosition(
        signal=Signal(is_long=True, entry=D("100"), stop=D("105"), target=D("130")),
        opened_at=T0, qty=D("1"), notional=D("100"), margin=D("10"),
        liq_price=D("0"), entry_fee=D("0"),
    )
    _arm_breakeven(position, bar(0, "200", "99"), config())

    assert not position.breakeven_armed
    assert position.stop == D("105")


def test_short_side_mirrors_the_long_one():
    def short_once(window):
        if len(window) != 3:
            return None
        return Signal(is_long=False, entry=D("100"), stop=D("110"), target=D("70"))

    bars = series(bar(0, "101", "88"), bar(1, "115", "99"))

    result = run(bars, short_once, sizer, config())
    trade = result.outcomes[0].trade
    assert trade.close_reason is CloseReason.BREAKEVEN
    assert trade.exit_price < D("100")


def test_a_winner_still_reaches_its_target():
    """Moving the stop must not prevent the trade from reaching the target."""
    bars = series(bar(0, "112", "99"), bar(1, "135", "105"))

    result = run(bars, long_once(), sizer, config())
    trade = result.outcomes[0].trade
    assert trade.close_reason is CloseReason.TARGET
    assert trade.exit_price == D("130")


# ---------------------------------------------------------------------------
# Time-only exit (DEC-033)
# ---------------------------------------------------------------------------
def test_signal_without_target_exits_only_by_time_or_stop():
    """A bar that breaks any "target" does not close the position: there is no target."""
    def time_only(window):
        if len(window) != 3:
            return None
        return Signal(is_long=True, entry=D("100"), stop=D("90"), target=None)

    bars = series(bar(0, "500", "99"), bar(1, "600", "99"))
    cfg = config(breakeven=None)
    cfg.time_stop = timedelta(hours=8)
    result = run(bars, time_only, sizer, cfg)
    trade = result.outcomes[0].trade
    assert trade.close_reason is CloseReason.TIME_STOP
    assert trade.target is None


def test_signal_without_target_still_hits_its_stop():
    def time_only(window):
        if len(window) != 3:
            return None
        return Signal(is_long=True, entry=D("100"), stop=D("90"), target=None)

    bars = series(bar(0, "101", "85"))
    result = run(bars, time_only, sizer, config(breakeven=None))
    assert result.outcomes[0].trade.close_reason is CloseReason.STOP


def test_order_plan_for_a_time_exit_has_a_stop_and_no_target():
    """DEC-041: time exit plan — entry and protective stop, no target."""
    from datetime import UTC, datetime

    from src.execution.plan import InstrumentSpec, OrderPlan, build

    spec = InstrumentSpec("SOLUSDT", D("0.01"), D("0.1"), D("0.1"), D("1000"), D("5"))
    result = build(
        Signal(is_long=True, entry=D("100"), stop=D("90"), target=None),
        spec=spec, qty=D("1"), moment=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert isinstance(result, OrderPlan)
    assert result.take_profit is None
    assert result.target_price is None
    assert result.stop_price <= D("100") and result.risk_per_unit <= D("10")
    assert len(result.orders) == 2
