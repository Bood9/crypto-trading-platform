"""Setup E: rule D, tested as intended. DEC-038.

One property is checked, the one E exists for: it differs
from D ONLY by the stop ceiling, and the ceiling is derived by a formula, not chosen.
Any second difference would make E a different hypothesis, and comparing it
with D would be impossible.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from src.core.config import load_settings
from src.strategy.setup_d import SetupDParams


def params(section: str) -> SetupDParams:
    return SetupDParams.from_config(load_settings().strategy[section])


def test_e_differs_from_d_only_by_the_stop_cap():
    d, e = asdict(params("setup_d")), asdict(params("setup_e"))
    different = {k for k in d if d[k] != e[k]}
    assert different == {"max_stop_pct"}, f"E also differs from D in: {different}"


def test_stop_cap_is_derived_from_the_deposit_not_chosen():
    """DEC-033 formula: risk per trade / exchange minimum order."""
    settings = load_settings()
    risk_usd = Decimal(str(settings.risk.equity_usd)) * Decimal(
        str(settings.risk.max_loss_per_trade_pct)
    ) / 100
    min_order = Decimal("5")
    assert params("setup_e").max_stop_pct == risk_usd / min_order * 100


def test_e_is_frozen_together_with_the_code_of_d():
    """E's logic lives in setup_d.py; otherwise an edit to D would change E
    without the freeze noticing."""
    from src.backtest.validation import frozen_code

    plan = load_settings().backtest.validation["setup_e"]
    assert "src/strategy/setup_d.py" in frozen_code("setup_e", tuple(plan.extra_code))


def test_e_has_its_own_conditions_identical_to_d():
    plans = load_settings().backtest.validation
    d, e = plans["setup_d"], plans["setup_e"]
    assert e.null_advantage_min_se == d.null_advantage_min_se == 2.0
    assert e.time_exit_move_floor and d.time_exit_move_floor
    assert e.windows["in_sample"] == d.windows["in_sample"]
    assert e.windows["recent_oos"] == d.windows["recent_oos"]
