"""Reconciliation of state with the exchange. Spec §8, P-001.

The most critical code in the system: it decides whether we know what we
have open. Each kind of discrepancy is checked separately, and also that
findings do not mask each other: they usually share a cause, and fixing
them one at a time means fixing for a long time.
"""

from __future__ import annotations

from decimal import Decimal

from src.execution.reconcile import (
    Action,
    Discrepancy,
    ExchangeOrder,
    ExchangePosition,
    LocalPosition,
    reconcile,
)

D = Decimal


def mine(symbol="SOLUSDT", *, is_long=True, qty="1") -> LocalPosition:
    return LocalPosition(
        symbol=symbol,
        is_long=is_long,
        qty=D(qty),
        stop_link_id=f"cb-stop-{symbol}",
        target_link_id=f"cb-tp-{symbol}",
    )


def theirs(symbol="SOLUSDT", *, is_long=True, qty="1") -> ExchangePosition:
    return ExchangePosition(
        symbol=symbol, is_long=is_long, qty=D(qty), entry_price=D("100")
    )


def protective(symbol="SOLUSDT") -> list[ExchangeOrder]:
    return [
        ExchangeOrder(f"cb-stop-{symbol}", symbol, D("1"), reduce_only=True),
        ExchangeOrder(f"cb-tp-{symbol}", symbol, D("1"), reduce_only=True),
    ]


def kinds(result) -> set[Discrepancy]:
    return {f.kind for f in result.findings}


# ---------------------------------------------------------------------------
# Consistent state
# ---------------------------------------------------------------------------
def test_matching_state_produces_no_findings():
    result = reconcile([mine()], [theirs()], protective())
    assert not result.findings
    assert not result.halt_required
    assert result.describe() == "state matches"


def test_empty_everything_is_consistent():
    assert not reconcile([], [], []).findings


# ---------------------------------------------------------------------------
# Position discrepancies
# ---------------------------------------------------------------------------
def test_position_on_exchange_we_do_not_know_about_halts():
    """Our model of the world is wrong, and the next decision will be made
    on a wrong model."""
    result = reconcile([], [theirs()], [])
    assert kinds(result) == {Discrepancy.ORPHAN_POSITION}
    assert result.halt_required


def test_position_we_think_we_have_but_exchange_does_not_halts():
    result = reconcile([mine()], [], protective())
    assert Discrepancy.PHANTOM_POSITION in kinds(result)
    assert result.halt_required


def test_size_mismatch_halts():
    result = reconcile([mine(qty="1")], [theirs(qty="0.7")], protective())
    assert Discrepancy.SIZE_MISMATCH in kinds(result)
    assert result.halt_required


def test_side_mismatch_halts_and_does_not_also_report_size():
    """Opposite sides are not "a different size", and reporting
    them under both codes would hide the cause behind the consequence."""
    result = reconcile([mine(is_long=True)], [theirs(is_long=False)], protective())
    assert Discrepancy.SIDE_MISMATCH in kinds(result)
    assert Discrepancy.SIZE_MISMATCH not in kinds(result)


def test_rounding_in_the_last_digit_is_not_a_discrepancy():
    """The exchange rounds to the lot step, and exact Decimal equality
    would catch a last-digit difference as a state discrepancy."""
    result = reconcile(
        [mine(qty="1.000000000000001")], [theirs(qty="1")], protective()
    )
    assert Discrepancy.SIZE_MISMATCH not in kinds(result)


# ---------------------------------------------------------------------------
# An unprotected position is the only case where we act first
# ---------------------------------------------------------------------------
def test_open_position_without_a_stop_demands_immediate_action():
    """Waiting for a human is more dangerous than acting: an unprotected position
    on leverage loses money every second."""
    only_target = [ExchangeOrder("cb-tp-SOLUSDT", "SOLUSDT", D("1"), True)]
    result = reconcile([mine()], [theirs()], only_target)

    assert Discrepancy.UNPROTECTED in kinds(result)
    assert result.unprotected
    assert result.unprotected[0].action is Action.PLACE_STOP_THEN_HALT
    assert result.halt_required


def test_protection_is_checked_even_when_sizes_agree():
    """A position can match to the last digit and still hang without a stop."""
    result = reconcile([mine(qty="1")], [theirs(qty="1")], [])
    assert Discrepancy.UNPROTECTED in kinds(result)


def test_missing_target_halts_but_is_not_urgent():
    only_stop = [ExchangeOrder("cb-stop-SOLUSDT", "SOLUSDT", D("1"), True)]
    result = reconcile([mine()], [theirs()], only_stop)

    assert Discrepancy.MISSING_ORDER in kinds(result)
    assert Discrepancy.UNPROTECTED not in kinds(result)
    assert result.halt_required
    assert not result.unprotected


# ---------------------------------------------------------------------------
# Foreign orders
# ---------------------------------------------------------------------------
def test_unknown_order_is_cancelled_without_halting():
    """A hanging reduce-only order will open an opposite position if
    the position is already closed."""
    orders = [*protective(), ExchangeOrder("foreign-1", "SOLUSDT", D("5"), True)]
    result = reconcile([mine()], [theirs()], orders)

    assert kinds(result) == {Discrepancy.ORPHAN_ORDER}
    assert result.to_cancel
    assert result.to_cancel[0].action is Action.CANCEL
    assert not result.halt_required


def test_orders_left_from_a_previous_life_are_cancelled():
    result = reconcile([], [], [ExchangeOrder("old", "BTCUSDT", D("1"), True)])
    assert kinds(result) == {Discrepancy.ORPHAN_ORDER}
    assert not result.halt_required


# ---------------------------------------------------------------------------
# Findings do not mask each other
# ---------------------------------------------------------------------------
def test_all_findings_are_reported_not_just_the_first():
    """Discrepancies usually share a cause, and fixing them one at a time means
    fixing for a long time."""
    local = [mine("SOLUSDT"), mine("ETHUSDT", qty="2")]
    exchange = [theirs("SOLUSDT", qty="0.5"), theirs("BTCUSDT")]
    orders = [ExchangeOrder("cb-stop-SOLUSDT", "SOLUSDT", D("1"), True)]

    result = reconcile(local, exchange, orders)

    assert kinds(result) == {
        Discrepancy.SIZE_MISMATCH,      # SOLUSDT diverged in size
        Discrepancy.MISSING_ORDER,      # SOLUSDT has no target
        Discrepancy.ORPHAN_POSITION,    # BTCUSDT on the exchange, not in ours
        Discrepancy.PHANTOM_POSITION,   # ETHUSDT in ours, not on the exchange
    }
    assert result.halt_required


def test_description_lists_every_finding():
    result = reconcile([mine()], [], [])
    text = result.describe()
    assert "PHANTOM_POSITION" in text
    assert "SOLUSDT" in text


def test_findings_are_stable_in_order():
    """The discrepancy report is read by a human at an unpleasant moment.
    The order must not depend on how the dictionary happened to be laid out."""
    local = [mine("ZUSDT"), mine("AUSDT")]
    first = reconcile(local, [], [])
    second = reconcile(list(reversed(local)), [], [])
    assert [f.symbol for f in first.findings] == [f.symbol for f in second.findings]
