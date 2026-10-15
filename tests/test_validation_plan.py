"""Validation plan: prohibitions in code, not by agreement. DEC-028, DEC-030.

Each prohibition is tested by what it actually forbids: a window is spent
once, the recent OOS only after a passed deep history, the trading
configuration only when frozen.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from src.backtest.validation import (
    FROZEN_SECTIONS,
    Ledger,
    frozen_drift,
    record,
    refusal,
)
from src.core.config import Settings

COMMIT = "a6ff492"


def entry(ledger: Ledger, period: str, *, passed: bool, commit: str = COMMIT) -> None:
    record(
        ledger, period=period, commit=commit, window=("2021-11-01", "2024-08-01"),
        passed=passed, thresholds=[], extra={},
    )


# ---------------------------------------------------------------------------
# Order and single use
# ---------------------------------------------------------------------------
def test_deep_history_is_allowed_first():
    assert refusal("deep_history", COMMIT, Ledger()) is None


def test_recent_oos_is_refused_before_deep_history():
    reason = refusal("recent_oos", COMMIT, Ledger())
    assert reason and "deep history" in reason


def test_recent_oos_is_refused_after_a_failed_deep_history():
    """DEC-028, rule 2: a failed deep history rejects the setup, and the
    recent OOS is kept for the next one."""
    ledger = Ledger()
    entry(ledger, "deep_history", passed=False)
    reason = refusal("recent_oos", COMMIT, ledger)
    assert reason and "failed" in reason


def test_recent_oos_is_allowed_after_a_passed_deep_history():
    ledger = Ledger()
    entry(ledger, "deep_history", passed=True)
    assert refusal("recent_oos", COMMIT, ledger) is None


@pytest.mark.parametrize("period", ["deep_history", "recent_oos"])
def test_every_window_is_spent_once(period):
    ledger = Ledger()
    entry(ledger, "deep_history", passed=True)
    if period == "recent_oos":
        entry(ledger, "recent_oos", passed=True)
    reason = refusal(period, COMMIT, ledger)
    assert reason and "once" in reason


def test_a_pass_under_another_configuration_does_not_count():
    """A pass under another configuration is a test of another hypothesis."""
    ledger = Ledger()
    entry(ledger, "deep_history", passed=True, commit="0000000")
    assert refusal("recent_oos", COMMIT, ledger) is not None


def test_unknown_window_is_refused():
    assert refusal("everything", COMMIT, Ledger()) is not None


def test_ledger_survives_a_round_trip(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = Ledger()
    entry(ledger, "deep_history", passed=True)
    ledger.save(path)
    again = Ledger.load(path)
    assert again.find("deep_history", COMMIT)["passed"] is True
    assert Ledger.load(tmp_path / "missing.json").entries == []


# ---------------------------------------------------------------------------
# Configuration freeze
# ---------------------------------------------------------------------------
BASE = {
    "risk": {"max_loss_per_trade_pct": 1.5},
    "costs": {"taker_fee_pct": 0.055},
    "strategy": {
        "setup_c": {"target_r": 2.0},
        "management": {"breakeven_at_r": 1.0},
        "min_target_r": 2.0,
        "min_move_pct": 1.6,
    },
    "data": {"streams": ["kline_4h"]},
}


def test_identical_configuration_has_no_drift():
    assert frozen_drift(BASE, copy.deepcopy(BASE)) == []


@pytest.mark.parametrize(
    "path,value",
    [
        (("strategy", "setup_c", "target_r"), 2.5),
        (("strategy", "management", "breakeven_at_r"), 0.5),
        (("risk", "max_loss_per_trade_pct"), 2.0),
        (("costs", "taker_fee_pct"), 0.02),
        (("strategy", "min_target_r"), 1.0),
    ],
)
def test_any_trading_parameter_change_is_drift(path, value):
    changed = copy.deepcopy(BASE)
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert frozen_drift(BASE, changed), f"{'.'.join(path)} changed unnoticed"


def test_non_trading_sections_do_not_count_as_drift():
    """Data collection and reports do not affect the hypothesis test."""
    changed = copy.deepcopy(BASE)
    changed["data"]["streams"].append("kline_1h")
    assert frozen_drift(BASE, changed) == []


def test_frozen_sections_cover_the_trading_path():
    names = {".".join(p) for p in FROZEN_SECTIONS}
    assert {"strategy.setup_c", "strategy.management", "risk", "costs"} <= names


# ---------------------------------------------------------------------------
# Strict config
# ---------------------------------------------------------------------------
@pytest.fixture
def raw():
    import pathlib

    import yaml

    return yaml.safe_load(pathlib.Path("config/settings.yaml").read_text())


def test_unknown_key_is_rejected_not_ignored(raw):
    """This is how a validation plan section was lost: by default pydantic
    silently drops unknown keys (DEC-030)."""
    broken = copy.deepcopy(raw)
    broken["risk"]["max_loss_per_trad_pct"] = 99  # typo
    with pytest.raises(ValidationError, match="max_loss_per_trad_pct"):
        Settings.model_validate(broken)


def test_validation_plans_actually_load(raw):
    plans = Settings.model_validate(raw).backtest.validation
    assert set(plans) >= {"setup_c", "setup_d"}
    assert plans["setup_c"].frozen_at_commit == "a6ff492"
    assert plans["setup_d"].null_advantage_min_se == 2.0
    assert plans["setup_d"].windows["recent_oos"].kind == "out_of_sample"


def test_overlap_with_an_out_of_sample_window_is_rejected(raw):
    broken = copy.deepcopy(raw)
    broken["backtest"]["validation"]["setup_c"]["windows"]["recent_oos"]["start"] = (
        "2024-01-01"
    )
    with pytest.raises(ValidationError, match="overlap"):
        Settings.model_validate(broken)


def test_sequence_may_only_name_declared_windows(raw):
    broken = copy.deepcopy(raw)
    broken["backtest"]["validation"]["setup_d"]["sequence"].append("everything")
    with pytest.raises(ValidationError, match="unknown windows"):
        Settings.model_validate(broken)


# ---------------------------------------------------------------------------
# DEC-034: out-of-sample is spent once for ALL setups
# ---------------------------------------------------------------------------
class _Window:
    def __init__(self, start, end, kind):
        self.start, self.end, self.kind = start, end, kind


class _Plan:
    def __init__(self, windows, sequence):
        self.windows = windows
        self.sequence = sequence


PLAN_D = _Plan(
    {
        "in_sample": _Window("2022-02-01", "2026-02-07", "in_sample"),
        "recent_oos": _Window("2026-02-07", "2026-09-01", "out_of_sample"),
    },
    ["in_sample", "recent_oos"],
)


def test_in_sample_may_overlap_another_setups_out_of_sample():
    """A period that was OOS for C legitimately serves as in-sample for D:
    in-sample is exactly where looking is allowed."""
    ledger = Ledger()
    entry(ledger, "deep_history", passed=False)  # C: 2021-11 … 2024-08, OOS
    assert refusal("in_sample", "d1", ledger, setup="setup_d", plan=PLAN_D) is None


def test_out_of_sample_spent_by_one_setup_is_closed_for_the_next():
    """Looking at an out-of-sample period turns it into in-sample for any
    subsequent hypothesis."""
    ledger = Ledger()
    record(ledger, period="in_sample", commit="d1", window=("2022-02-01", "2026-02-07"),
           passed=True, thresholds=[], extra={}, setup="setup_d", kind="in_sample")
    record(ledger, period="recent_oos", commit="d1", window=("2026-02-07", "2026-09-01"),
           passed=False, thresholds=[], extra={}, setup="setup_d", kind="out_of_sample")

    plan_e = _Plan(
        {
            "in_sample": _Window("2022-02-01", "2026-02-07", "in_sample"),
            "recent_oos": _Window("2026-02-07", "2026-09-01", "out_of_sample"),
        },
        ["in_sample", "recent_oos"],
    )
    record(ledger, period="in_sample", commit="e1", window=("2022-02-01", "2026-02-07"),
           passed=True, thresholds=[], extra={}, setup="setup_e", kind="in_sample")
    reason = refusal("recent_oos", "e1", ledger, setup="setup_e", plan=plan_e)
    assert reason and "already out-of-sample" in reason


def test_in_sample_gate_closes_the_out_of_sample_window():
    ledger = Ledger()
    record(ledger, period="in_sample", commit="d1", window=("2022-02-01", "2026-02-07"),
           passed=False, thresholds=[], extra={}, setup="setup_d", kind="in_sample")
    reason = refusal("recent_oos", "d1", ledger, setup="setup_d", plan=PLAN_D)
    assert reason and "failed" in reason


def test_out_of_sample_needs_the_in_sample_first():
    reason = refusal("recent_oos", "d1", Ledger(), setup="setup_d", plan=PLAN_D)
    assert reason and "in_sample" in reason


def test_setups_do_not_share_per_setup_records():
    """A window run by one setup does not count as a run by another."""
    ledger = Ledger()
    entry(ledger, "deep_history", passed=True, commit="d1")  # this is C
    assert refusal("in_sample", "d1", ledger, setup="setup_d", plan=PLAN_D) is None


def test_legacy_c_entry_counts_as_spent_out_of_sample():
    """A Setup C record from before DEC-034 had no window kind: it was a test
    window, that is, out-of-sample."""
    ledger = Ledger(entries=[{
        "period": "deep_history", "commit": "a6ff492",
        "window": ["2022-02-01", "2024-08-01"], "passed": False,
    }])
    assert ledger.spent_out_of_sample() == [("setup_c", "2022-02-01", "2024-08-01")]


# ---------------------------------------------------------------------------
# Freeze: by the setup's own section
# ---------------------------------------------------------------------------
def test_freeze_covers_the_setups_own_section_and_file():
    from src.backtest.validation import frozen_code, frozen_sections

    assert ("strategy", "setup_d") in frozen_sections("setup_d")
    assert ("strategy", "setup_c") not in frozen_sections("setup_d")
    assert "src/strategy/setup_d.py" in frozen_code("setup_d")
    assert "src/strategy/setup_a.py" not in frozen_code("setup_d")


# ---------------------------------------------------------------------------
# Code freeze
# ---------------------------------------------------------------------------
def test_code_drift_detects_changes_since_an_old_commit():
    """A fix in the signal function changes the hypothesis without touching the config."""
    from src.backtest.validation import code_drift

    # Before DEC-025 the engine did not accrue funding.
    assert "src/backtest/engine.py" in code_drift("d29cb5f")



def test_unknown_commit_is_an_error_not_a_pass():
    """P-001: failing to compare does not mean "matched"."""
    from src.backtest.validation import code_drift

    with pytest.raises(ValueError):
        code_drift("deadbeefdeadbeef")
