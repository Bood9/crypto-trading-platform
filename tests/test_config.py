"""Configuration tests.

An inconsistent config must fail on load, not after two weeks
of trading on puzzling Risk Manager behaviour.
"""

from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from src.core.config import DEFAULT_SETTINGS_PATH, Settings, load_settings


@pytest.fixture
def raw() -> dict:
    return yaml.safe_load(DEFAULT_SETTINGS_PATH.read_text(encoding="utf-8"))


def test_shipped_settings_are_valid():
    settings = load_settings()
    # DEC-038: the minimum deposit raised from $40 to $100.
    assert settings.risk.equity_usd == 100.0
    assert settings.universe.crypto_only is True
    assert settings.universe.include_delisted is True


def test_risk_limits_match_spec():
    r = load_settings().risk
    assert r.margin_per_trade_pct == 17.5
    # It was 6.0 from the spec. Lowered on two already fixed thresholds:
    # P-007 gives a ceiling of 2.5%, the drawdown threshold p95 < 40% gives 1.5%
    # (at 2.0% the measurement gives 42.0%). See DEC-019.
    assert r.max_loss_per_trade_pct == 1.5
    assert r.max_concurrent_positions == 4  # DEC-050
    assert r.max_total_margin_pct == 87.5
    assert r.leverage_cap == 10
    assert r.min_liq_buffer_ratio == 3.0


def test_risk_and_loss_limits_must_survive_eight_losses(raw):
    """P-007. Two numbers, reasonable separately, are incompatible together:
    6% per trade with a 20% weekly limit shuts down the week with four
    losses in a row."""
    raw = copy.deepcopy(raw)
    raw["risk"]["max_loss_per_trade_pct"] = 6.0
    with pytest.raises(ValidationError, match="P-007"):
        Settings.model_validate(raw)


def test_risk_that_kills_a_day_in_two_trades_is_rejected(raw):
    """With a limit of two simultaneous positions this is a halt after
    the very first bad day."""
    raw = copy.deepcopy(raw)
    raw["risk"]["max_loss_per_trade_pct"] = 2.5
    raw["risk"]["daily_loss_limit_pct"] = 4.0
    raw["risk"]["weekly_loss_limit_pct"] = 20.0
    with pytest.raises(ValidationError, match="P-007"):
        Settings.model_validate(raw)


def test_all_slots_stopped_in_one_day_must_stay_under_the_daily_limit(raw):
    """The check counts by the declared number of slots, not by two."""
    raw = copy.deepcopy(raw)
    raw["risk"]["max_concurrent_positions"] = 7  # 7 × 1.5% > 10%
    raw["risk"]["max_total_margin_pct"] = 100.0
    raw["risk"]["margin_per_trade_pct"] = 10.0
    with pytest.raises(ValidationError, match="7 concurrent positions"):
        Settings.model_validate(raw)


def test_margin_cap_must_hold_every_declared_slot(raw):
    """DEC-050: 4 slots with a 35% margin ceiling — the last slot would exist
    only in the config, while MAX_MARGIN would reject it."""
    raw = copy.deepcopy(raw)
    raw["risk"]["max_total_margin_pct"] = 35.0
    with pytest.raises(ValidationError, match="does not fit 4 positions"):
        Settings.model_validate(raw)


def test_the_boundary_value_is_accepted(raw):
    """The rule gives exactly 2.5% with a 20% weekly limit, and this
    value must pass: otherwise the guard demands a buffer
    it never declared."""
    raw = copy.deepcopy(raw)
    raw["risk"]["max_loss_per_trade_pct"] = 2.5
    assert Settings.model_validate(raw).risk.max_loss_per_trade_pct == 2.5


def test_weekly_limit_below_daily_is_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["risk"]["weekly_loss_limit_pct"] = 5.0
    with pytest.raises(ValidationError, match="weekly_loss_limit_pct"):
        Settings.model_validate(raw)


def test_total_margin_below_per_trade_is_rejected(raw):
    """Such a config would silently reject every trade."""
    raw = copy.deepcopy(raw)
    raw["risk"]["max_total_margin_pct"] = 10.0
    with pytest.raises(ValidationError, match="no trade can pass"):
        Settings.model_validate(raw)


def test_liq_buffer_below_one_is_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["risk"]["min_liq_buffer_ratio"] = 0.5
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_stream_requirements_must_reference_known_streams(raw):
    """A typo in a stream name would otherwise quietly disable the availability check."""
    raw = copy.deepcopy(raw)
    raw["data"]["stream_requirements"]["setup_a"] = ["kline_15m", "oi_typo"]
    with pytest.raises(ValidationError, match="unknown"):
        Settings.model_validate(raw)


def test_every_setup_requirement_is_covered_by_declared_streams():
    data = load_settings().data
    declared = set(data.streams)
    for required in data.stream_requirements.model_dump().values():
        assert set(required) <= declared


def test_collapse_fixtures_are_present():
    """LUNA and FTT are mandatory fixtures of the engine stress test (Phase 2)."""
    symbols = {f.symbol for f in load_settings().backtest.collapse_fixtures}
    assert {"LUNAUSDT", "FTTUSDT"} <= symbols


def test_short_history_policy_is_explicit():
    p = load_settings().backtest.short_history
    assert p.require_full_in_sample_for_fitting is True
    assert p.min_months_before_oos == 6
    assert p.log_each_inclusion is True


def test_universe_selection_is_lagged():
    """Selection by turnover of the traded period would be lookahead."""
    assert load_settings().universe.selection_lag_months >= 1


def test_liquidity_floor_is_absolute_not_only_rank():
    assert load_settings().universe.min_avg_daily_turnover_usd > 0


def test_round_trip_cost_is_below_minimum_target():
    """The target must be a multiple of the costs, otherwise we work for the exchange."""
    s = load_settings()
    assert s.costs.round_trip_taker_pct < s.strategy["min_move_pct"]


def test_bybit_base_url_switches_to_testnet():
    b = load_settings().bybit
    assert b.base_url(testnet=False) == b.rest_base_url
    assert b.base_url(testnet=True) == b.testnet_base_url


# ---------------------------------------------------------------------------
# Walk-forward (DEC-011)
# ---------------------------------------------------------------------------
def test_walk_forward_gives_enough_windows():
    """18 windows over a 24-month period with a minimum of 12."""
    bt = load_settings().backtest
    wf = bt.walk_forward
    total = bt.in_sample_months + bt.out_of_sample_months
    windows = (total - wf.train_months - wf.test_months) // wf.step_months + 1

    assert wf.enabled
    assert windows >= wf.min_windows
    assert windows == 18


def test_walk_forward_window_must_fit_the_period(raw):
    """A window longer than the period is a config error, not an empty result."""
    raw = copy.deepcopy(raw)
    raw["backtest"]["walk_forward"]["train_months"] = 30
    with pytest.raises(ValidationError, match="does not fit in the period"):
        Settings.model_validate(raw)


def test_too_few_windows_is_rejected(raw):
    """Conclusions from three windows are unreliable — the config must not allow it."""
    raw = copy.deepcopy(raw)
    raw["backtest"]["walk_forward"]["train_months"] = 18
    raw["backtest"]["walk_forward"]["step_months"] = 3
    with pytest.raises(ValidationError, match="windows"):
        Settings.model_validate(raw)


def test_acceptance_thresholds_are_unchanged_by_walk_forward():
    """DEC-011 is a diagnostic, not a relaxation.

    The test guards exactly this: if the acceptance thresholds drift along
    with adding a third slice, curve fitting has happened.
    """
    bt = load_settings().backtest
    assert bt.in_sample_months == 18
    assert bt.out_of_sample_months == 6
    assert bt.short_history.warn_if_trade_share_above_pct == 10.0


def test_liquidity_correlation_check_is_enabled():
    """A strategy living on high liquidity must be visible
    before Phase 3: we will start trading in quiet mode."""
    wf = load_settings().backtest.walk_forward
    assert wf.report_e_vs_liquidity_correlation
    assert 0 < wf.liquidity_correlation_warn_above <= 1


def test_marginal_entrants_are_tracked_not_excluded():
    """The symbols legitimately pass selection — they are not excluded in advance.

    But their contribution is counted separately: it is skewed in OOS (18 occurrences
    versus 5 in IS).
    """
    me = load_settings().backtest.marginal_entrants
    assert me.report_trade_share
    assert me.report_e_without_them
    assert len(me.symbols()) == 22


def test_walk_forward_overlap_is_declared():
    """The windows share 5 of the 6 training months.

    The estimates are not independent; the warning must make it into the report,
    otherwise in a month someone will compute a t-statistic.
    """
    wf = load_settings().backtest.walk_forward
    assert wf.windows_overlap
    assert wf.overlap_warning_in_report
    assert wf.shared_months == 5


# ---------------------------------------------------------------------------
# Coverage threshold (DEC-012)
# ---------------------------------------------------------------------------
def test_coverage_threshold_is_fixed():
    """Fixed before the run on a par with the acceptance thresholds."""
    data = load_settings().data
    assert data.min_stream_coverage == 0.80
    assert data.coverage_sensitivity == [0.70, 0.80, 0.90]


def test_base_coverage_is_inside_the_sweep(raw):
    """Otherwise the base run would end up outside the sweep."""
    raw = copy.deepcopy(raw)
    raw["data"]["min_stream_coverage"] = 0.65
    with pytest.raises(ValidationError, match="coverage_sensitivity"):
        Settings.model_validate(raw)


def test_coverage_values_must_be_fractions(raw):
    raw = copy.deepcopy(raw)
    raw["data"]["coverage_sensitivity"] = [0.7, 0.8, 80]
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_every_universe_knob_has_a_sweep():
    """Slippage, coverage, the turnover threshold and WIDTH are the four knobs
    of composition. Each must have a sweep: a knob without a sensitivity
    check is a hidden degree of freedom.

    The check used to name three knobs and only required liquidity to be
    positive. Universe width was not considered a knob at all — and
    it turned out to be the binding one: top_n cut off more symbols
    than the turnover threshold (DEC-015).
    """
    s = load_settings()
    assert len(s.costs.slippage_sensitivity) >= 3
    assert len(s.data.coverage_sensitivity) >= 3
    assert len(s.universe.min_turnover_sensitivity) >= 3
    assert len(s.universe.top_n_sensitivity) >= 3


def test_width_knobs_must_appear_in_their_own_sweeps(raw):
    """A sweep around a number that is not in it matches
    nothing."""
    spoiled = copy.deepcopy(raw)
    spoiled["universe"]["top_n"] = 37
    with pytest.raises(ValidationError, match="top_n_sensitivity"):
        Settings.model_validate(spoiled)

    spoiled = copy.deepcopy(raw)
    spoiled["universe"]["min_avg_daily_turnover_usd"] = 33_000_000
    with pytest.raises(ValidationError, match="min_turnover_sensitivity"):
        Settings.model_validate(spoiled)
