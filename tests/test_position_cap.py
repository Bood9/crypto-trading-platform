"""Position size cap. DEC-040.

The wish: a position within $5-30 on a $100 deposit. The lower bound
is held by the exchange minimum order, the upper one by this cap. Checked:
it trims rather than rejects, and the stop loss from trimming
only decreases.
"""

from __future__ import annotations

from decimal import Decimal

from src.core.config import load_settings
from src.core.models import RejectionCode
from src.risk.position_sizer import SizingInput, calculate_size

D = Decimal


def sizing(*, stop: str, cap: str | None = "30", equity: str = "100") -> SizingInput:
    return SizingInput(
        equity=D(equity),
        entry_price=D("100"),
        stop_price=D(stop),
        mmr=D("0.005"),
        qty_step=D("0.001"),
        min_order_qty=D("0.001"),
        min_notional=D("5"),
        margin_pct=D("17.5"),
        max_loss_pct=D("1.5"),
        leverage_cap=D("10"),
        min_liq_buffer_ratio=D("3"),
        max_position_pct=D(cap) if cap is not None else None,
    )


def test_tight_stop_without_cap_gives_a_large_leveraged_position():
    """What the cap exists for: a 1% stop → a $150 position with leverage."""
    result = calculate_size(sizing(stop="99", cap=None))
    assert result.approved
    assert result.notional > D("100")


def test_cap_cuts_the_position_instead_of_rejecting_it():
    result = calculate_size(sizing(stop="99"))
    assert result.approved
    assert result.notional <= D("30")
    assert result.notional > D("29")


def test_cut_position_loses_less_at_the_stop():
    uncapped = calculate_size(sizing(stop="99", cap=None))
    capped = calculate_size(sizing(stop="99"))
    assert capped.loss_at_stop < uncapped.loss_at_stop
    assert capped.loss_at_stop <= D("1.5")


def test_cap_does_not_touch_positions_already_under_it():
    """A normal Setup E trade (~$8) is unchanged."""
    wide = sizing(stop="82")  # 18% stop → position ~$8.3
    assert calculate_size(wide).notional == calculate_size(
        sizing(stop="82", cap=None)
    ).notional


def test_minimum_order_still_rejects_what_the_exchange_would():
    """The lower bound is the exchange minimum; the cap does not replace it."""
    result = calculate_size(sizing(stop="65"))  # 35% stop → position < $5
    assert not result.approved
    assert result.rejection in {RejectionCode.MIN_NOTIONAL, RejectionCode.LIQ_BUFFER}


def test_cap_scales_with_the_deposit():
    result = calculate_size(sizing(stop="99", equity="200"))
    assert result.notional <= D("60")


def test_configured_cap_is_thirty_percent():
    assert load_settings().risk.max_position_pct == 30.0
