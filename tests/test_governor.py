"""Portfolio risk. Spec §5.

This checks the code standing between the signal and the money. Six rejection codes
were declared in the enum and had no implementation, so here
each one is covered: a missing check looks the same in the report
as a check that never fired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.models import RejectionCode
from src.risk.governor import (
    Anchors,
    GovernorLimits,
    HeldPosition,
    PortfolioState,
    TradingMode,
    check,
    resume,
)

D = Decimal
T0 = datetime(2026, 3, 4, 12, 0, tzinfo=UTC)  # Wednesday


LIMITS = GovernorLimits(
    max_concurrent_positions=2,
    max_total_margin_pct=D("35"),
    max_correlated_positions=1,
    correlation_threshold=D("0.7"),
    daily_loss_limit_pct=D("10"),
    weekly_loss_limit_pct=D("20"),
)


def uncorrelated(_a: str, _b: str) -> Decimal:
    return D("0.1")


def perfectly_correlated(_a: str, _b: str) -> Decimal:
    return D("0.95")


def unknown(_a: str, _b: str) -> None:
    return None


def position(symbol: str, *, margin: str = "7", is_long: bool = True) -> HeldPosition:
    return HeldPosition(
        symbol=symbol,
        is_long=is_long,
        margin=D(margin),
        qty=D("1"),
        entry=D("100"),
        opened_at=T0,
    )


def fresh(equity: str = "40") -> PortfolioState:
    return PortfolioState.start(D(equity), T0)


def ask(state, *, symbol="SOLUSDT", is_long=True, margin="7", moment=T0, corr=uncorrelated):
    return check(
        state,
        LIMITS,
        symbol=symbol,
        is_long=is_long,
        margin=D(margin),
        moment=moment,
        correlation=corr,
    )


# ---------------------------------------------------------------------------
# Basic admission
# ---------------------------------------------------------------------------
def test_clean_portfolio_lets_a_trade_through():
    assert ask(fresh()).approved


def test_negative_start_is_refused_outright():
    with pytest.raises(ValueError):
        PortfolioState.start(D("0"), T0)


# ---------------------------------------------------------------------------
# MAX_POSITIONS
# ---------------------------------------------------------------------------
def test_position_cap_is_enforced():
    state = fresh()
    state.open(position("BTCUSDT"))
    state.open(position("ETHUSDT"))
    verdict = ask(state)
    assert verdict.rejection is RejectionCode.MAX_POSITIONS


def test_second_entry_into_the_same_symbol_is_not_a_second_position():
    """This is not a second position but an increase of the first one bypassing the sizer."""
    state = fresh()
    state.open(position("SOLUSDT"))
    verdict = ask(state, symbol="SOLUSDT")
    assert verdict.rejection is RejectionCode.MAX_POSITIONS
    assert "already open" in verdict.note


# ---------------------------------------------------------------------------
# MAX_MARGIN
# ---------------------------------------------------------------------------
def test_total_margin_cap_is_enforced():
    state = fresh("40")  # ceiling 35% = 14
    state.open(position("BTCUSDT", margin="7"))
    assert ask(state, margin="7").approved  # exactly 14 — at the boundary
    assert ask(state, margin="7.01").rejection is RejectionCode.MAX_MARGIN


def test_margin_cap_uses_current_equity_not_the_initial():
    """Equity in drawdown shrinks the allowed margin. Computing the ceiling
    from the starting deposit would mean increasing the share of risk exactly
    as things get worse."""
    state = fresh("40")
    state.open(position("BTCUSDT", margin="7"))
    # Drawdown 7.5%: below the daily limit, otherwise that one would fire,
    # and it is not what is checked here.
    state.apply_pnl(D("-3"), T0)  # equity 37, ceiling 12.95
    assert ask(state, margin="7").rejection is RejectionCode.MAX_MARGIN


def test_ruin_trips_the_weekly_limit_before_anything_else():
    """An account at zero is not "margin does not fit", it is the end of the week."""
    state = fresh("40")
    state.apply_pnl(D("-40"), T0)
    assert ask(state).rejection is RejectionCode.WEEKLY_STOP


def test_exhausted_equity_is_reported_as_balance_not_as_margin():
    """If the loss limits were reset by hand but there is still no money,
    the cause must be called by its own name."""
    state = fresh("40")
    state.apply_pnl(D("-40"), T0)
    state = resume(state, T0)  # the reference points moved to zero
    assert ask(state).rejection is RejectionCode.INSUFFICIENT_BALANCE


# ---------------------------------------------------------------------------
# CORRELATION
# ---------------------------------------------------------------------------
def test_correlated_same_direction_is_one_bet():
    state = fresh()
    state.open(position("BTCUSDT", is_long=True))
    verdict = ask(state, is_long=True, corr=perfectly_correlated)
    assert verdict.rejection is RejectionCode.CORRELATION
    assert "one bet" in verdict.note


def test_correlated_opposite_direction_is_allowed():
    """A long and a short on two pairs moving together is a hedge, not a bet."""
    state = fresh()
    state.open(position("BTCUSDT", is_long=True))
    assert ask(state, is_long=False, corr=perfectly_correlated).approved


def test_negative_correlation_with_opposite_directions_is_also_one_bet():
    """The config says "when the direction matches" and describes only
    half of the case. A short on one pair and a long on another at correlation −0.9
    win and lose together just like two longs at +0.9."""
    state = fresh()
    state.open(position("BTCUSDT", is_long=True))
    verdict = ask(state, is_long=False, corr=lambda a, b: D("-0.9"))
    assert verdict.rejection is RejectionCode.CORRELATION


def test_negative_correlation_with_same_direction_is_allowed():
    state = fresh()
    state.open(position("BTCUSDT", is_long=True))
    assert ask(state, is_long=True, corr=lambda a, b: D("-0.9")).approved


def test_correlation_below_threshold_does_not_block():
    state = fresh()
    state.open(position("BTCUSDT"))
    assert ask(state, corr=lambda a, b: D("0.69")).approved
    assert ask(state, corr=lambda a, b: D("0.70")).rejection is RejectionCode.CORRELATION


def test_unknown_correlation_is_refused_not_assumed_zero():
    """P-001. We cannot confirm the bet's independence — so
    we do not enter. Substituting zero would pass off "we don't know"
    as "not related"."""
    state = fresh()
    state.open(position("BTCUSDT"))
    verdict = ask(state, corr=unknown)
    assert verdict.rejection is RejectionCode.CORRELATION
    assert "unknown" in verdict.note


def test_correlation_is_not_consulted_on_an_empty_portfolio():
    """Nothing to correlate with is no reason to reject."""
    assert ask(fresh(), corr=unknown).approved


# ---------------------------------------------------------------------------
# DAILY_STOP / WEEKLY_STOP
# ---------------------------------------------------------------------------
def test_daily_loss_limit_switches_the_system_off():
    state = fresh("40")
    state.apply_pnl(D("-4"), T0)  # exactly 10%
    verdict = ask(state)
    assert verdict.rejection is RejectionCode.DAILY_STOP
    assert state.mode is TradingMode.OFF_DAILY


def test_daily_limit_clears_itself_on_the_next_utc_day():
    state = fresh("40")
    state.apply_pnl(D("-4"), T0)
    assert not ask(state).approved

    tomorrow = T0 + timedelta(days=1)
    verdict = ask(state, moment=tomorrow)
    assert verdict.approved
    assert state.mode is TradingMode.ON
    # The reference point moved to the current equity.
    assert state.anchors.day_equity == D("36")


def test_weekly_limit_does_not_clear_itself():
    """A week of losses is a reason to look at the system,
    not to wait for Monday."""
    state = fresh("40")
    state.apply_pnl(D("-8"), T0)  # 20%
    assert ask(state).rejection is RejectionCode.WEEKLY_STOP
    assert state.mode is TradingMode.OFF_WEEKLY

    next_week = T0 + timedelta(days=8)
    assert ask(state, moment=next_week).rejection is RejectionCode.WEEKLY_STOP
    assert state.mode is TradingMode.OFF_WEEKLY


def test_manual_resume_moves_the_anchors_forward():
    """Otherwise the system would switch on and immediately off: the loss
    relative to the old reference point has not gone anywhere."""
    state = fresh("40")
    state.apply_pnl(D("-8"), T0)
    ask(state)
    assert state.mode is TradingMode.OFF_WEEKLY

    state = resume(state, T0 + timedelta(hours=1))
    assert state.mode is TradingMode.ON
    assert state.anchors.week_equity == D("32")
    assert ask(state, moment=T0 + timedelta(hours=1)).approved


def test_weekly_limit_outranks_the_daily_one():
    """Both are breached — report the one that is harder to lift."""
    state = fresh("40")
    state.apply_pnl(D("-9"), T0)  # 22.5% both for the day and for the week
    assert ask(state).rejection is RejectionCode.WEEKLY_STOP


def test_profit_does_not_arm_the_limits():
    state = fresh("40")
    state.apply_pnl(D("+10"), T0)
    assert state.day_loss_pct() < 0
    assert ask(state).approved


# ---------------------------------------------------------------------------
# Day and week boundaries
# ---------------------------------------------------------------------------
def test_day_rolls_at_utc_midnight_not_at_a_local_one():
    state = fresh("40")
    state.apply_pnl(D("-4"), T0)
    ask(state)

    # Still the same UTC day, although in another time zone it is already tomorrow.
    late = datetime(2026, 3, 4, 23, 59, tzinfo=UTC)
    assert ask(state, moment=late).rejection is RejectionCode.DAILY_STOP

    midnight = datetime(2026, 3, 5, 0, 0, tzinfo=UTC)
    assert ask(state, moment=midnight).approved


def test_week_boundary_uses_iso_calendar():
    """The ISO year differs from the calendar year at the junction: 29 December 2025
    belongs to the first week of 2026, and the (year, number) pair accounts for it."""
    late_december = datetime(2025, 12, 29, tzinfo=UTC)
    anchors = Anchors.at(late_december, D("40"))
    assert anchors.week == (2026, 1)
    assert anchors.day.year == 2025


def test_rolling_over_a_week_also_rolls_the_day():
    state = fresh("40")
    state.apply_pnl(D("-2"), T0)
    state.roll(T0 + timedelta(days=8))
    assert state.anchors.day_equity == D("38")
    assert state.anchors.week_equity == D("38")


def test_state_survives_being_switched_off_for_several_days():
    """A new day starts by itself, not by a timer inside
    a running process."""
    state = fresh("40")
    state.apply_pnl(D("-4"), T0)
    ask(state)
    assert state.mode is TradingMode.OFF_DAILY

    much_later = T0 + timedelta(days=3)
    assert ask(state, moment=much_later).approved


# ---------------------------------------------------------------------------
# Order of checks
# ---------------------------------------------------------------------------
def test_off_mode_outranks_every_other_check():
    """In OFF the limits are no longer considered: the reason for switching off
    matters more than the portfolio composition."""
    state = fresh("40")
    state.mode = TradingMode.OFF_MANUAL
    state.open(position("BTCUSDT"))
    state.open(position("ETHUSDT"))
    verdict = ask(state, corr=unknown)
    assert verdict.rejection is RejectionCode.MODE_OFF


def test_loss_limits_outrank_portfolio_composition():
    state = fresh("40")
    state.apply_pnl(D("-4"), T0)
    state.open(position("BTCUSDT"))
    state.open(position("ETHUSDT"))
    assert ask(state).rejection is RejectionCode.DAILY_STOP


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------
def test_opening_a_duplicate_symbol_is_a_programming_error():
    state = fresh()
    state.open(position("BTCUSDT"))
    with pytest.raises(ValueError):
        state.open(position("BTCUSDT"))


def test_closing_an_absent_symbol_is_a_programming_error():
    with pytest.raises(KeyError):
        fresh().close("BTCUSDT")


def test_total_margin_sums_open_positions():
    state = fresh()
    state.open(position("BTCUSDT", margin="7"))
    state.open(position("ETHUSDT", margin="5.5"))
    assert state.total_margin == D("12.5")
    assert state.close("BTCUSDT").symbol == "BTCUSDT"
    assert state.total_margin == D("5.5")


def test_fourth_slot_survives_a_loss_under_the_real_config():
    """DEC-050: the margin of held positions was computed from equity at the time of their
    entry. With a ceiling of exactly 4 × 17.5% the fourth slot disappeared after
    any loss; with headroom up to the weekly limit (/0.8) — it does not."""
    from src.core.config import load_settings

    limits = GovernorLimits.from_config(load_settings().risk)
    held = {s: position(s, margin="17.5") for s in ("A", "B", "C")}
    for equity in ("100", "85", "80.5"):  # the loss is from past weeks, not today's
        state = PortfolioState(equity=D(equity), anchors=Anchors.at(T0, D(equity)))
        state.positions.update(held)
        margin = D(equity) * D("17.5") / 100
        verdict = check(state, limits, symbol="D", is_long=True, margin=margin, moment=T0,
                        correlation=uncorrelated)
        assert verdict.approved, (equity, verdict)
    full = PortfolioState(equity=D("100"), anchors=Anchors.at(T0, D("100")))
    full.positions.update(held | {"D": position("D", margin="17.5")})
    fifth = check(full, limits, symbol="E", is_long=True, margin=D("17.5"), moment=T0,
                  correlation=uncorrelated)
    assert fifth.rejection is RejectionCode.MAX_POSITIONS
