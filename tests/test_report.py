"""Status report tests.

The main rule is checked right here: an uncomputed value is shown
as "not computed", not as zero. A report that shows zero where there is
no data is worse than a missing one: it creates confidence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.report.content import GUARDS, decisions_block, guards_block, limitations_block
from src.report.status import UNKNOWN, Block, fmt_num, render


def test_missing_value_says_so_instead_of_zero():
    """P-001 applied to the report."""
    assert fmt_num(None) == UNKNOWN
    assert fmt_num(float("nan")) == UNKNOWN
    assert fmt_num(0) == "0", "a real zero stays zero"


def test_empty_block_is_marked_not_hidden():
    """A hidden empty block is indistinguishable from a forgotten one."""
    html = render([Block(title="Empty", table=[])], generated=datetime.now(UTC))
    assert "Empty" in html
    assert UNKNOWN in html


def test_report_is_self_contained():
    """Not a single external load: the page opens from disk."""
    html = render([Block(title="T", rows=[("a", 1)])], generated=datetime.now(UTC))
    for marker in ("http://", "https://", "<script", "<link"):
        assert marker not in html, f"external dependency: {marker}"


def test_generation_time_is_present():
    moment = datetime(2026, 8, 8, 12, 30, tzinfo=UTC)
    html = render([], generated=moment)
    assert "08.08.2026 12:30" in html


def test_values_are_escaped():
    """Data from the DB must not end up in the markup as code."""
    html = render(
        [Block(title="T", rows=[("key", "<img src=x onerror=alert(1)>")])],
        generated=datetime.now(UTC),
    )
    assert "onerror=alert" not in html or "&lt;img" in html


# ---------------------------------------------------------------------------
# Mechanical guarantees
# ---------------------------------------------------------------------------
def test_every_guard_names_its_location():
    """A guard without a location is useless: in half a year nobody finds it."""
    for g in GUARDS:
        if g.enforced:
            assert g.where, f"{g.principle}: guard exists, no location"


def test_unguarded_principle_is_shown_as_such():
    """A rule without a guard is a note, and that must be visible."""
    unguarded = [g for g in GUARDS if not g.enforced]
    assert unguarded, "the test goes stale when every principle gets a guard"

    block = guards_block()
    text = str(block.table)
    assert "no guard" in text


def test_guards_cover_all_principles():
    """Every P-XXX from DECISIONS.md must be in the guarantees table."""
    from src.core.config import PROJECT_ROOT

    decisions = (PROJECT_ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    import re

    principles = set(re.findall(r"^### (P-\d+) —", decisions, re.MULTILINE))
    listed = {g.principle for g in GUARDS}

    assert principles <= listed, f"no guarantee: {principles - listed}"


# ---------------------------------------------------------------------------
# Blocks from documents
# ---------------------------------------------------------------------------
def test_decisions_block_reads_actual_file():
    block = decisions_block()
    assert block.table
    codes = {row[0] for row in block.table}
    assert "DEC-001" in codes
    assert "DEC-012" in codes


def test_limitations_block_lists_all_nine():
    block = limitations_block()
    assert block.table
    numbers = {row[0] for row in block.table}
    assert len(numbers) >= 9, f"limitations found: {len(numbers)}"


# ---------------------------------------------------------------------------
# P-005: the target and the time stop must be compatible
# ---------------------------------------------------------------------------
def _outcome(reason: str):
    from datetime import timedelta
    from decimal import Decimal

    from src.backtest.accounting import CloseReason, Trade, evaluate

    opened = datetime(2026, 1, 1, tzinfo=UTC)
    return evaluate(
        Trade(
            symbol="X",
            opened_at=opened,
            closed_at=opened + timedelta(hours=4),
            is_long=True,
            entry=Decimal("100"),
            stop=Decimal("95"),
            exit_price=Decimal("101"),
            qty=Decimal("1"),
            notional=Decimal("100"),
            margin=Decimal("10"),
            close_reason=CloseReason(reason),
        )
    )


def test_run_with_unreachable_target_is_rejected():
    """The Setup B case: the target was hit in 1.1% of trades.

    The result looked complete: 1494 trades, 47% win rate,
    all blocks computed. Nothing in the final numbers said
    that the wrong hypothesis was being tested.
    """
    from src.backtest.report import RunInvalid, assert_run_is_valid, exit_breakdown

    outcomes = [_outcome("TIME_STOP") for _ in range(90)]
    outcomes += [_outcome("STOP") for _ in range(9)]
    outcomes += [_outcome("TARGET")]

    breakdown = exit_breakdown(outcomes)
    assert breakdown.target_rate == pytest.approx(0.01)
    assert not breakdown.valid

    with pytest.raises(RunInvalid, match="P-005"):
        assert_run_is_valid(breakdown, "setup_b")


def test_run_with_reachable_target_passes():
    from src.backtest.report import assert_run_is_valid, exit_breakdown

    outcomes = [_outcome("TARGET") for _ in range(20)]
    outcomes += [_outcome("STOP") for _ in range(50)]
    outcomes += [_outcome("TIME_STOP") for _ in range(30)]

    breakdown = exit_breakdown(outcomes)
    assert breakdown.valid
    assert_run_is_valid(breakdown, "setup_x")


def test_empty_run_is_invalid_not_silently_ok():
    """P-001: no trades, no right to a conclusion."""
    from src.backtest.report import RunInvalid, assert_run_is_valid, exit_breakdown

    with pytest.raises(RunInvalid, match="nothing to measure"):
        assert_run_is_valid(exit_breakdown([]), "setup_x")


def test_threshold_is_ten_percent():
    """The threshold is fixed: relaxing it after the result is fitting."""
    from src.backtest.report import MIN_TARGET_HIT_RATE

    assert MIN_TARGET_HIT_RATE == 0.10


# ---------------------------------------------------------------------------
# P-006: the target must pay for itself
# ---------------------------------------------------------------------------
def _settings_with(min_target_r: float, min_move_pct: float):
    from src.core.config import get_settings

    settings = get_settings().model_copy(deep=True)
    settings.strategy["min_target_r"] = min_target_r
    settings.strategy["min_move_pct"] = min_move_pct
    return settings


def _priced_outcome(entry: str, stop: str, target: str | None):
    from decimal import Decimal

    from src.backtest.accounting import Trade, evaluate

    return evaluate(
        Trade(
            symbol="TESTUSDT",
            opened_at=datetime(2026, 1, 1, tzinfo=UTC),
            closed_at=datetime(2026, 1, 2, tzinfo=UTC),
            is_long=True,
            entry=Decimal(entry),
            stop=Decimal(stop),
            exit_price=Decimal(entry),
            qty=Decimal(1),
            notional=Decimal(entry),
            margin=Decimal(1),
            target=Decimal(target) if target is not None else None,
        )
    )


def test_target_economics_reads_the_target_from_the_trade():
    """With a VWAP exit the target is not a parameter but a value computed
    by the market, and different for each trade."""
    from src.backtest.report import target_economics

    e = target_economics(
        [_priced_outcome("100", "90", "110"), _priced_outcome("100", "90", "130")],
        _settings_with(2.0, 1.6),
    )
    assert e.trades == 2
    assert e.median_r == pytest.approx(2.0)
    assert e.median_move_pct == pytest.approx(20.0)


def test_close_target_fails_even_with_a_high_hit_rate():
    """The flip side of P-005 that no guard caught:
    the target is hit almost always, and pays for neither the stop nor the fee."""
    from src.backtest.report import RunInvalid, assert_target_is_payable, target_economics

    e = target_economics([_priced_outcome("100", "90", "100.4")] * 5, _settings_with(2.0, 1.6))
    assert e.median_r == pytest.approx(0.04)
    with pytest.raises(RunInvalid, match="does not pay for itself"):
        assert_target_is_payable(e, "test")


def test_target_that_clears_both_floors_passes():
    from src.backtest.report import assert_target_is_payable, target_economics

    e = target_economics([_priced_outcome("100", "90", "125")] * 5, _settings_with(2.0, 1.6))
    assert e.valid
    assert_target_is_payable(e, "test")


def test_target_beyond_the_ratio_but_inside_the_costs_still_fails():
    """The cost is not the ratio to the stop by itself. The stop can be
    arbitrarily narrow, and then 3R turns out smaller than the round-trip fee."""
    from src.backtest.report import RunInvalid, assert_target_is_payable, target_economics

    e = target_economics(
        [_priced_outcome("100", "99.9", "100.3")] * 5, _settings_with(2.0, 1.6)
    )
    assert e.pays_for_the_ratio
    assert not e.pays_for_the_costs
    with pytest.raises(RunInvalid, match="round-trip|fee"):
        assert_target_is_payable(e, "test")


def test_run_without_targets_is_not_silently_approved():
    """P-001: "no targets were set" is not "the targets are fine"."""
    from src.backtest.report import RunInvalid, assert_target_is_payable, target_economics

    e = target_economics([_priced_outcome("100", "90", None)], _settings_with(2.0, 1.6))
    assert e.trades == 0
    with pytest.raises(RunInvalid):
        assert_target_is_payable(e, "test")


def test_both_guards_are_independent():
    """P-005 and P-006 catch opposite errors and do not substitute
    for each other: a run must pass both."""
    from src.backtest.report import (
        ExitBreakdown,
        assert_run_is_valid,
        assert_target_is_payable,
        target_economics,
    )

    # The target is far and almost never hit: P-006 is satisfied, P-005 is not.
    far = ExitBreakdown(counts={"TARGET": 1, "TIME_STOP": 99}, total=100)
    assert not far.valid
    assert target_economics([_priced_outcome("100", "90", "130")], _settings_with(2.0, 1.6)).valid

    # The target is near and always hit: P-005 is satisfied, P-006 is not.
    near = ExitBreakdown(counts={"TARGET": 90, "STOP": 10}, total=100)
    assert near.valid
    assert_run_is_valid(near, "test")
    with pytest.raises(Exception, match="does not pay for itself"):
        assert_target_is_payable(
            target_economics([_priced_outcome("100", "90", "100.4")], _settings_with(2.0, 1.6)),
            "test",
        )


def test_p005_refuses_a_verdict_on_a_tiny_sample():
    """"Not computed" is not "PASS" (P-002).

    The trigger sweep gave E = +2.475R, a 100% win rate
    and a "PASS" verdict on ONE trade. A rate estimated from a handful of
    observations says nothing about the construction.
    """
    from src.backtest.report import (
        MIN_TRADES_FOR_VERDICT,
        ExitBreakdown,
        RunInvalid,
        assert_run_is_valid,
    )

    perfect = ExitBreakdown(counts={"TARGET": 1}, total=1)
    assert perfect.target_rate == 1.0
    assert perfect.valid, "the rate itself passes the threshold, and that means nothing"
    with pytest.raises(RunInvalid, match="NOT DECIDED"):
        assert_run_is_valid(perfect, "test")

    enough = ExitBreakdown(
        counts={"TARGET": 5, "TIME_STOP": MIN_TRADES_FOR_VERDICT - 5},
        total=MIN_TRADES_FOR_VERDICT,
    )
    assert_run_is_valid(enough, "test")
