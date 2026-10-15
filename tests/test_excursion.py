"""Measuring the price excursion after entry.

The value is diagnostic and therefore dangerous: it looks forward from the entry.
What is checked here is not only the arithmetic but also that the measurement answers
the question it exists for — which target is attainable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.backtest.accounting import CloseReason, Trade, evaluate
from src.backtest.engine import BacktestConfig, MarketBar, Signal, run
from src.backtest.excursion import (
    attainability,
    distribution,
    measure,
    target_at_quantile,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
DAY = timedelta(hours=24)


def bar(i: int, high: str, low: str, close: str = "100") -> MarketBar:
    return MarketBar(T0 + H * i, D("100"), D(high), D(low), D(close), D("1"))


def long_trade(**overrides) -> Trade:
    fields = {
        "symbol": "TESTUSDT",
        "opened_at": T0,
        "closed_at": None,
        "is_long": True,
        "entry": D("100"),
        "stop": D("90"),
        "exit_price": D("100"),
        "qty": D("1"),
        "notional": D("100"),
        "margin": D("10"),
        "close_reason": CloseReason.TIME_STOP,
    }
    fields.update(overrides)
    return Trade(**fields)


def outcomes_of(trade: Trade) -> list:
    return [evaluate(trade)]


# ---------------------------------------------------------------------------
# Window boundaries
# ---------------------------------------------------------------------------
def test_measurement_starts_after_the_entry_bar():
    """Entry and exit within one bar would mean knowing its future:
    the entry bar is not part of the measurement."""
    bars = [bar(0, "500", "100"), bar(1, "110", "99"), bar(2, "105", "99")]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    # Risk 10, the maximum after entry is 110 → 1.0R. The entry bar's high (500)
    # is not counted, otherwise it would be 40R.
    assert e.attainable_r == D("1")


def test_horizon_includes_the_bar_that_triggers_the_time_stop():
    """The engine checks the target on this bar before the time stop — so
    the target can still be taken on it."""
    bars = [bar(i, "101", "99") for i in range(24)] + [bar(24, "150", "99")]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    assert e.bars_held == 24
    assert e.attainable_r == D("5")


def test_horizon_stops_at_the_time_stop_and_not_later():
    bars = [bar(i, "101", "99") for i in range(25)] + [bar(25, "900", "99")]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    assert e.attainable_r == D("0.1")


def test_trade_whose_horizon_runs_off_the_data_is_excluded():
    """P-001: a trade with an incomplete horizon is not a trade
    with zero excursion."""
    bars = [bar(i, "101", "99") for i in range(5)]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    assert e.truncated
    assert not e.valid


def test_trade_whose_entry_bar_is_missing_is_reported_not_guessed():
    (e,) = measure([bar(3, "101", "99")], outcomes_of(long_trade()), DAY)
    assert not e.valid
    assert e.invalid_reason is not None


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------
def test_the_bar_that_hits_the_stop_does_not_count_as_favourable():
    """Within a bar the order of events is unknown. Assuming the maximum
    came first is the same overstatement `_check_exit` guards against."""
    bars = [bar(0, "100", "100"), bar(1, "200", "80"), bar(2, "101", "99")] + [
        bar(i, "101", "99") for i in range(3, 25)
    ]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    assert e.attainable_r == D("0"), "counted the high of the bar that stopped out"
    # The horizon does not know about the stop — there the same 200 gives 10R.
    assert e.horizon_r == D("10")
    assert e.adverse_r == D("2")


def test_movement_after_the_stop_is_invisible_to_the_attainable_figure():
    bars = [bar(0, "100", "100"), bar(1, "99", "80")] + [
        bar(i, "300", "99") for i in range(2, 25)
    ]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    assert e.attainable_r == D("0")
    assert e.horizon_r == D("20")


# ---------------------------------------------------------------------------
# Meaning of the value
# ---------------------------------------------------------------------------
def test_attainable_is_not_capped_by_the_current_target():
    """The key property. A measurement clipped by the active target could not
    discover that a larger one is attainable — and the whole point would be lost."""
    bars = [bar(0, "100", "100"), bar(1, "120", "99")] + [
        bar(i, "101", "99") for i in range(2, 25)
    ]
    trade = long_trade(
        target=D("105"), exit_price=D("105"), close_reason=CloseReason.TARGET
    )
    (e,) = measure(bars, outcomes_of(trade), DAY)

    # The trade closed at the 0.5R target, while the market gave 2R.
    assert e.attainable_r == D("2")


def test_a_move_that_never_goes_our_way_is_negative_not_zero():
    """The tail of the distribution is exactly what is measured: replacing such
    trades with zero means erasing exactly what is being measured."""
    bars = [bar(0, "100", "100")] + [bar(i, "95", "94") for i in range(1, 25)]
    trade = long_trade(stop=D("50"))
    (e,) = measure(bars, outcomes_of(trade), DAY)

    assert e.attainable_r < 0


def test_short_side_is_measured_downwards():
    bars = [bar(0, "100", "100")] + [bar(i, "101", "80") for i in range(1, 25)]
    trade = long_trade(is_long=False, stop=D("110"))
    (e,) = measure(bars, outcomes_of(trade), DAY)

    assert e.attainable_r == D("2")
    assert e.adverse_r == D("0.1")


def test_bars_to_peak_points_at_the_bar_where_the_maximum_happened():
    bars = [bar(0, "100", "100")] + [
        bar(i, "150" if i == 7 else "101", "99") for i in range(1, 25)
    ]
    (e,) = measure(bars, outcomes_of(long_trade()), DAY)

    assert e.bars_to_peak == 7


# ---------------------------------------------------------------------------
# Link between the measurement and a real run
# ---------------------------------------------------------------------------
def test_attainability_equals_the_target_hit_rate_of_an_actual_run():
    """What the measurement is for.

    The share of trades whose excursion reached T must match the share
    of target exits in a run with target T. If it does not, the target cannot be
    chosen from the distribution — a backtest would be needed for every value.
    """
    series = [bar(0, "100", "100")]
    for i in range(1, 40):
        series.append(bar(i, "100.8", "99.9", "100.4"))
    series.append(bar(40, "118", "99.9", "117"))
    series += [bar(i, "101", "99.9") for i in range(41, 60)]

    def once(window):
        if len(window) != 21:
            return None
        return Signal(
            is_long=True, entry=D("100"), stop=D("90"), target=D("110")
        )

    cfg = BacktestConfig(
        initial_equity=D("1000"),
        taker_fee_pct=D("0"),
        slippage_pct=D("0"),
        time_stop=timedelta(hours=30),
        warmup_bars=20,
    )
    result = run(series, once, lambda s, p: (D("1"), D("100"), D("10")), cfg)
    assert result.outcomes

    (e,) = measure(series, result.outcomes, cfg.time_stop)
    assert e.valid

    for target_r, _hits, share in attainability([float(e.attainable_r)]):
        signal_target = D("100") + D(str(target_r)) * D("10")

        def probe(window, level=signal_target):
            if len(window) != 21:
                return None
            return Signal(is_long=True, entry=D("100"), stop=D("90"), target=level)

        probed = run(series, probe, lambda s, p: (D("1"), D("100"), D("10")), cfg)
        took_target = sum(
            1 for o in probed.outcomes if o.trade.close_reason is CloseReason.TARGET
        )
        assert took_target == (1 if share else 0), f"target {target_r}R"


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def test_distribution_of_an_empty_sample_answers_nothing():
    d = distribution([])
    assert d.n == 0
    assert d.median is None
    assert "empty" in d.describe()


def test_distribution_quantiles():
    d = distribution([float(i) for i in range(1, 101)])
    assert d.n == 100
    assert 24 <= d.p25 <= 26
    assert 50 <= d.median <= 51
    assert 74 <= d.p75 <= 76
    assert d.maximum == 100.0


def test_attainability_counts_reaching_the_level_not_exceeding_it():
    rows = dict((t, share) for t, _n, share in attainability([1.0, 1.0, 3.0, 0.0]))
    assert rows[1.0] == 0.75
    assert rows[1.25] == 0.25


def test_target_at_quantile_rounds_down_to_the_grid():
    """Rounding up is not allowed: the target would become unattainable exactly
    for the trades it was chosen by."""
    values = [float(i) / 10 for i in range(1, 101)]  # 0.1 … 10.0
    assert target_at_quantile(values, 0.75) == 7.5
    assert target_at_quantile([1.7] * 10, 0.75) == 1.5
    assert target_at_quantile([], 0.75) is None
    assert target_at_quantile([0.01] * 5, 0.75) == 0.25
