"""The signal funnel.

What is checked first is not the arithmetic but two properties without which
the funnel is harmful: it must not change the signal's behaviour and must not
silently lose a stage.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from src.backtest.funnel import SETUP_A_STAGES, Funnel, Stage, make_funnel
from src.strategy.setup_a import build_atr_series, make_signal_fn
from tests.test_setup_a import (
    HOUR,
    cascade_window_for_long,
    context_for_long,
    make_params,
)


def test_unknown_stage_is_an_error_not_a_new_counter():
    """A stage missing from the markup is a typo. Creating a counter for it
    on the fly means losing it in the report."""
    f = make_funnel()
    with pytest.raises(KeyError, match="not declared"):
        f.enter("magic_stage")


def test_duplicate_stage_keys_are_refused():
    with pytest.raises(ValueError, match="repeat"):
        Funnel(stages=(Stage("a", "A"), Stage("a", "Another A")))


def test_counters_must_only_decrease():
    """A monotonicity violation is a markup defect, not a property of the data:
    a stage cannot accept more candidates than the previous one."""
    f = make_funnel()
    for _ in range(10):
        f.enter("bars")
    for _ in range(3):
        f.enter("warmup")
    assert f.is_monotone()

    for _ in range(5):
        f.enter("oi_drop")
    assert not f.is_monotone()


def test_rows_give_share_of_start_and_share_of_previous_stage():
    f = make_funnel()
    for _ in range(1000):
        f.enter("bars")
    for _ in range(100):
        f.enter("warmup")
    for _ in range(10):
        f.enter("oi_drop")

    rows = {title: (n, share, step) for title, n, share, step in f.rows()}
    _n, share, step = rows["OI contraction over an hour"]
    assert share == pytest.approx(0.01)
    assert step == pytest.approx(0.1)


def test_merge_refuses_differently_marked_funnels():
    """Adding funnels with different markup would give a number that
    cannot be read."""
    a = make_funnel()
    b = Funnel(stages=(Stage("bars", "bars"),))
    with pytest.raises(ValueError, match="labelled differently"):
        a.merge(b)


def test_merge_adds_counters_and_samples():
    a, b = make_funnel(), make_funnel()
    a.enter("bars")
    a.record_stop(Decimal("5"))
    b.enter("bars")
    b.enter("warmup")
    b.record_funded(Decimal("3"))

    a.merge(b)
    assert a.counts["bars"] == 2
    assert a.counts["warmup"] == 1
    assert a.stop_pct == [5.0]
    assert a.stop_pct_funded == [3.0]


def test_every_stage_starts_at_zero_not_missing():
    """P-001: "nobody passed the stage" and "there is no stage" are different things."""
    f = make_funnel()
    assert set(f.counts) == {s.key for s in SETUP_A_STAGES}
    assert all(v == 0 for v in f.counts.values())


# ---------------------------------------------------------------------------
# Observing does not change the observed
# ---------------------------------------------------------------------------
def test_funnel_does_not_change_the_signal():
    """The counter must be a pure observation. If the signal with the funnel
    and without it diverge, the funnel measures its own influence."""
    params = make_params()
    window = cascade_window_for_long(params)
    context = context_for_long(window)
    series = build_atr_series(window, params.stop_atr_period, HOUR)

    without = make_signal_fn(context, params, series)(window)
    funnel = make_funnel()
    with_funnel = make_signal_fn(context, params, series, funnel)(window)

    assert without == with_funnel
    assert without is not None
    assert funnel.counts["signal"] == 1
    assert funnel.is_monotone()


def test_funnel_marks_the_stage_where_the_candidate_died():
    """A bar that did not pass the OI contraction must be visible exactly there."""
    from src.strategy.setup_b import SymbolContext

    params = make_params()
    window = cascade_window_for_long(params)
    flat_oi = SymbolContext(
        oi_ts=[window[-1].ts - HOUR, window[-1].ts],
        oi_values=[Decimal("1000"), Decimal("1000")],  # no contraction
        funding_ts=[],
        funding_rates=[],
    )
    funnel = make_funnel()
    series = build_atr_series(window, params.stop_atr_period, HOUR)

    assert make_signal_fn(flat_oi, params, series, funnel)(window) is None
    assert funnel.counts["warmup"] == 1
    assert funnel.counts["oi_drop"] == 0


def test_short_window_dies_at_warmup():
    params = make_params()
    funnel = make_funnel()
    window = cascade_window_for_long(params)[:5]
    context = context_for_long(window)

    assert make_signal_fn(context, params, None, funnel)(window) is None
    assert funnel.counts["bars"] == 1
    assert funnel.counts["warmup"] == 0


def test_stop_distance_is_recorded_for_surviving_signals():
    params = make_params()
    window = cascade_window_for_long(params)
    funnel = make_funnel()
    series = build_atr_series(window, params.stop_atr_period, HOUR)
    signal = make_signal_fn(context_for_long(window), params, series, funnel)(window)

    assert signal is not None
    assert len(funnel.stop_pct) == 1
    expected = abs(signal.entry - signal.stop) / signal.entry * 100
    assert funnel.stop_pct[0] == pytest.approx(float(expected))


def test_target_stage_separates_vwap_rejections():
    """Filtering out "VWAP behind the entry" must be visible as a separate stage,
    not dissolve into the total of bars that did not fire."""
    params = make_params()
    window = cascade_window_for_long(params)
    last = window[-1]
    window[-1] = replace(last, close=Decimal("99.5"))
    funnel = make_funnel()
    series = build_atr_series(window, params.stop_atr_period, HOUR)

    assert make_signal_fn(context_for_long(window), params, series, funnel)(window) is None
    assert funnel.counts["risk_positive"] == 1
    assert funnel.counts["target"] == 0


def test_stage_order_matches_the_order_of_checks():
    """The markup is part of the contract: a reordered stage breaks reading
    the funnel without breaking a single calculation."""
    keys = [s.key for s in SETUP_A_STAGES]
    assert keys.index("oi_drop") < keys.index("candle_range")
    assert keys.index("candle_range") < keys.index("volume_z")
    assert keys.index("volume_z") < keys.index("funding")
    assert keys.index("signal") < keys.index("funded")
    assert keys.index("funded") < keys.index("in_universe")
    assert timedelta(0) == timedelta(0)
