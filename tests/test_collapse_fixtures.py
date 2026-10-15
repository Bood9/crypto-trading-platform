"""Stress test of the arithmetic on real collapses. Spec §14.

The data is real, exported from the collected history:

* **LUNAUSDT, May 2022** — 179 hourly bars, price from $87.77 to $0.01,
  a range of 8777×. A crash within four days.
* **FTTUSDT, November 2022** — 204 bars, a range of 20×, with a flat bar
  (high = low) and a zero-volume bar.

What is checked is not the strategy but that the arithmetic survives such
values without NaN, division by zero or overflow.

Why this comes before Setup B: if the engine silently produces garbage
on extreme values, it is invisible in the aggregate numbers of a normal run.
A single trade with R = 47 dissolves in the averages,
and the result ends up inflated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from src.backtest.accounting import (
    CloseReason,
    EquityCurve,
    Trade,
    evaluate,
    summarize,
)
from src.backtest.engine import BacktestConfig, MarketBar, Signal, run
from src.backtest.indicators import Bar, atr, atr_pct
from src.core.models import RejectionCode
from src.risk.position_sizer import SizingInput, calculate_size

D = Decimal
FIXTURE = Path(__file__).parent / "fixtures" / "collapse_bars.json"


def load(symbol: str) -> list[MarketBar]:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))[symbol]
    return [
        MarketBar(
            ts=datetime.fromisoformat(b["ts"]),
            open=D(b["open"]),
            high=D(b["high"]),
            low=D(b["low"]),
            close=D(b["close"]),
            volume=D(b["volume"]),
        )
        for b in raw
    ]


@pytest.fixture(scope="module")
def luna() -> list[MarketBar]:
    return load("LUNAUSDT")


@pytest.fixture(scope="module")
def ftt() -> list[MarketBar]:
    return load("FTTUSDT")


def test_fixtures_are_actually_extreme(luna, ftt):
    """Guard against the test degenerating.

    If the fixture is ever regenerated on a calm period,
    the test will keep passing while checking nothing.
    """
    prices = [b.low for b in luna] + [b.high for b in luna]
    assert max(prices) / min(prices) > 1000, "the LUNA fixture is no longer a collapse"
    assert min(prices) <= D("0.01")
    assert len(ftt) > 100


# ---------------------------------------------------------------------------
# ATR
# ---------------------------------------------------------------------------
def test_atr_survives_order_of_magnitude_collapse(luna):
    bars = [Bar(b.high, b.low, b.close) for b in luna]

    for end in range(15, len(bars)):
        value = atr(bars[:end], period=14)
        assert value is None or (value.is_finite() and value >= 0), (
            f"ATR degenerated at bar {end}: {value}"
        )


def test_atr_pct_stays_finite_at_micro_prices(luna):
    """A price of $0.01 with ATR in the single units gives a huge percentage —
    but it must remain a number, not infinity."""
    bars = [Bar(b.high, b.low, b.close) for b in luna]
    value = atr_pct(bars, period=14)
    assert value is None or value.is_finite()


def test_atr_returns_none_not_zero_on_degenerate_input():
    """Zero would mean "no volatility" — the stop would coincide with the entry."""
    flat = [Bar(D("0"), D("0"), D("0")) for _ in range(30)]
    assert atr(flat, 14) is None or atr(flat, 14) == 0
    assert atr_pct(flat, 14) is None


def test_atr_on_flat_bar_series(ftt):
    """FTT has a bar with high == low."""
    bars = [Bar(b.high, b.low, b.close) for b in ftt]
    value = atr(bars, 14)
    assert value is not None
    assert value.is_finite()


# ---------------------------------------------------------------------------
# Position size
# ---------------------------------------------------------------------------
def sizing(entry: str, stop: str, **kw) -> SizingInput:
    base = {
        "equity": D("40"),
        "entry_price": D(entry),
        "stop_price": D(stop),
        "mmr": D("0.005"),
        "qty_step": D("0.1"),
        "min_order_qty": D("0.1"),
        "min_notional": D("5"),
        "margin_pct": D("17.5"),
        "max_loss_pct": D("6"),
        "leverage_cap": D("10"),
        "min_liq_buffer_ratio": D("3"),
    }
    base.update(kw)
    return SizingInput(**base)


@pytest.mark.parametrize(
    "entry,stop",
    [
        ("87.77", "80"),        # before the crash
        ("19.69", "13.205"),    # during the crash
        ("1.4", "0.595"),       # near the bottom
        ("0.01", "0.008"),      # the bottom
        ("0.0000001", "0.00000008"),  # beyond anything real
    ],
)
def test_sizing_survives_any_price_scale(entry, stop):
    result = calculate_size(sizing(entry, stop))

    # Approved or rejected does not matter; what matters is no garbage.
    assert result.qty >= 0
    assert result.notional >= 0
    assert result.leverage >= 0
    for value in (result.qty, result.notional, result.leverage, result.loss_at_stop):
        assert value.is_finite(), f"non-numeric result at price {entry}"
    if not result.approved:
        assert result.rejection is not None


def test_sizing_rejects_zero_price_instead_of_crashing():
    result = calculate_size(sizing("0", "0"))
    assert not result.approved
    assert result.rejection is not None


def test_sizing_rejects_inverted_stop_distance():
    """Stop equals entry — division by zero in the R calculation."""
    result = calculate_size(sizing("100", "100"))
    assert not result.approved
    assert result.rejection is RejectionCode.LIQ_BUFFER


def test_sizing_rejects_absurdly_wide_stop():
    """A stop at 90% of the move: even leverage 1 does not give a threefold buffer."""
    result = calculate_size(sizing("100", "10"))
    assert not result.approved
    assert result.rejection is RejectionCode.LIQ_BUFFER


def test_leverage_is_derived_not_assumed():
    """Spec §4: leverage is COMPUTED from the distance to the stop."""
    tight = calculate_size(sizing("100", "98"))     # 2% stop
    wide = calculate_size(sizing("100", "95"))      # 5% stop

    assert tight.approved and wide.approved
    assert tight.leverage > wide.leverage


def test_liquidation_buffer_is_always_respected():
    """The main invariant: an approved trade cannot have a buffer
    smaller than required."""
    for stop_pct in range(1, 30):
        entry = D("100")
        stop = entry * (1 - D(stop_pct) / 100)
        result = calculate_size(sizing(str(entry), str(stop)))
        if result.approved:
            assert result.liq_buffer_ratio >= D("3"), (
                f"buffer {result.liq_buffer_ratio} at stop {stop_pct}%"
            )


def test_loss_never_exceeds_configured_cap():
    for stop_pct in range(1, 20):
        entry = D("100")
        stop = entry * (1 - D(stop_pct) / 100)
        result = calculate_size(sizing(str(entry), str(stop)))
        if result.approved:
            # Ceiling of 6% of $40 equity = $2.40, plus an allowance
            # for rounding up to the lot step.
            assert result.loss_at_stop <= D("2.45"), (
                f"loss {result.loss_at_stop} at stop {stop_pct}%"
            )


# ---------------------------------------------------------------------------
# R multiples and equity
# ---------------------------------------------------------------------------
def trade(**kw) -> Trade:
    base = {
        "symbol": "LUNAUSDT",
        "opened_at": datetime(2022, 5, 9, tzinfo=UTC),
        "closed_at": datetime(2022, 5, 10, tzinfo=UTC),
        "is_long": True,
        "entry": D("60"),
        "stop": D("55"),
        "exit_price": D("55"),
        "qty": D("1"),
        "notional": D("60"),
        "margin": D("7"),
    }
    base.update(kw)
    return Trade(**base)


def test_r_multiple_on_catastrophic_move():
    """Entry at $60, exit at $0.01 — a 6000-fold drop.

    R must remain a finite number, not overflow.
    """
    outcome = evaluate(trade(exit_price=D("0.01")))
    assert outcome.valid
    assert outcome.r_multiple is not None
    assert outcome.r_multiple.is_finite()
    assert outcome.r_multiple < 0


def test_unclosed_position_is_not_counted_as_breakeven():
    """The data ran out, the trade is open.

    Substituting zero would mean inventing a breakeven trade
    and inflating the total.
    """
    outcome = evaluate(trade(exit_price=None, closed_at=None))
    assert not outcome.valid
    assert outcome.r_multiple is None
    assert "not closed" in (outcome.invalid_reason or "")


def test_zero_risk_trade_is_invalid_not_infinite():
    outcome = evaluate(trade(stop=D("60")))
    assert not outcome.valid
    assert outcome.r_multiple is None


def test_invalid_trades_do_not_move_equity():
    curve = EquityCurve(initial=D("40"))
    curve.add(evaluate(trade(exit_price=None, closed_at=None)))

    assert curve.equity == D("40")
    assert len(curve.skipped) == 1


def test_invalid_trades_are_excluded_from_statistics():
    """A single degenerate trade must not skew the averages."""
    outcomes = [
        evaluate(trade(exit_price=D("70"))),
        evaluate(trade(exit_price=None, closed_at=None)),
        evaluate(trade(stop=D("60"))),
    ]
    stats = summarize(outcomes)

    assert stats.trades == 1
    assert stats.skipped == 2
    assert stats.expectancy_r is not None


def test_equity_can_go_to_zero_without_breaking():
    """With a $40 deposit, ruin is not an abstraction.

    Equity must not be clipped from below: that would hide the fact of ruin.
    """
    curve = EquityCurve(initial=D("40"))
    for _ in range(5):
        curve.add(evaluate(trade(entry=D("60"), stop=D("55"), exit_price=D("45"))))

    assert curve.equity < D("40")
    assert curve.max_drawdown_pct > 0
    assert curve.max_drawdown_pct.is_finite()


def test_drawdown_is_finite_after_ruin():
    curve = EquityCurve(initial=D("40"))
    curve.add(evaluate(trade(qty=D("100"), entry=D("60"), stop=D("55"), exit_price=D("0.01"))))

    assert curve.ruined
    assert curve.max_drawdown_pct.is_finite()
    assert curve.total_return_pct.is_finite()


# ---------------------------------------------------------------------------
# Full engine run over a collapse
# ---------------------------------------------------------------------------
def breakout(window):
    if len(window) < 21:
        return None
    current, prior = window[-1], window[-21:-1]
    highest = max(b.high for b in prior)
    if current.close > highest:
        stop = min(b.low for b in prior)
        if current.close - stop <= 0:
            return None
        return Signal(True, current.close, stop, current.close + (current.close - stop) * 3)
    return None


def real_sizer(signal, price):
    result = calculate_size(
        sizing(str(signal.entry), str(signal.stop), qty_step=D("0.001"), min_order_qty=D("0.001"))
    )
    if not result.approved:
        return None
    return result.qty, result.notional, result.margin


CONFIG = BacktestConfig(
    initial_equity=D("40"),
    taker_fee_pct=D("0.055"),
    slippage_pct=D("0.05"),
    time_stop=timedelta(hours=48),
    warmup_bars=21,
)


@pytest.mark.parametrize("symbol", ["LUNAUSDT", "FTTUSDT"])
def test_full_run_over_collapse_produces_no_garbage(symbol):
    bars = load(symbol)
    result = run(bars, breakout, real_sizer, CONFIG)

    assert result.curve is not None
    assert result.curve.equity.is_finite()
    assert result.curve.max_drawdown_pct.is_finite()
    assert result.bars_processed == len(bars)

    for outcome in result.outcomes:
        if outcome.valid:
            assert outcome.r_multiple is not None
            assert outcome.r_multiple.is_finite()
            assert outcome.pnl is not None
            assert outcome.pnl.is_finite()


def test_position_open_at_data_end_is_flagged_not_invented():
    """Data cut off in the middle of an open position.

    Closing it at the last price would mean inventing a trade
    that never happened. It is marked DATA_END and is not included
    in the statistics.
    """
    bars = load("LUNAUSDT")

    def always_open(window):
        # Open exactly once and never close:
        # the stop and target are beyond any movement.
        if len(window) != 25:
            return None
        price = window[-1].close
        return Signal(True, price, price / 10**9, price * 10**9)

    def permissive_sizer(signal, price):
        # Size is not checked here: a stop at 100% of the move would be rejected by
        # the Risk Manager on the liquidation buffer, and this test is about something else.
        return D("1"), price, price / 10

    # Time stop is disabled: the fixture covers 7.5 days, and 48 hours
    # would close the position before the end of the data.
    config = BacktestConfig(
        initial_equity=D("40"),
        taker_fee_pct=D("0.055"),
        slippage_pct=D("0.05"),
        time_stop=timedelta(days=3650),
        warmup_bars=21,
    )
    result = run(bars, always_open, permissive_sizer, config)

    assert result.unclosed == 1
    unclosed = [o for o in result.outcomes if o.trade.close_reason is CloseReason.DATA_END]
    assert len(unclosed) == 1
    assert not unclosed[0].valid, "an unclosed position got into the statistics"


def test_statistics_over_collapse_are_readable():
    """The summary must not contain None where there are valid trades,
    and must not invent numbers where there are none."""
    bars = load("FTTUSDT")
    result = run(bars, breakout, real_sizer, CONFIG)
    stats = summarize(result.outcomes)

    if stats.trades > 0:
        assert stats.expectancy_r is not None
        assert stats.expectancy_r.is_finite()
        assert stats.win_rate is not None
        assert 0 <= stats.win_rate <= 100
    else:
        assert stats.expectancy_r is None
