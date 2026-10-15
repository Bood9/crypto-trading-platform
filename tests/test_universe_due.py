"""When to build the new month's universe. Per a finding from re-checking
the night of 2026-09-28: on October 2 the cycle would have had no universe."""

from __future__ import annotations

from datetime import UTC, datetime

from src.cli import due_universe_months

SEPT = {"2026-08", "2026-09"}


def at(day: str, hm: str = "00:10") -> datetime:
    return datetime.fromisoformat(f"{day}T{hm}:00+00:00").astimezone(UTC)


def test_mid_month_nothing_is_due():
    assert due_universe_months(at("2026-09-28"), SEPT) == ([], [])


def test_last_day_of_month_the_next_is_needed_but_too_early():
    """September 30: tomorrow's decision (bar 30.09) is still September, but
    the day after's is not our concern; the September selection is not over yet."""
    assert due_universe_months(at("2026-09-30"), SEPT) == ([], [])


def test_first_of_month_the_new_month_is_due_and_buildable():
    """On October 1 the decision is still based on September (bar 30.09), but
    tomorrow's is on October. September is over, so October can and must be built."""
    assert due_universe_months(at("2026-10-01"), SEPT) == (["2026-10"], [])


def test_missed_first_of_month_is_caught_up_on_the_second():
    assert due_universe_months(at("2026-10-02", "05:00"), SEPT) == (["2026-10"], [])


def test_built_month_is_not_rebuilt():
    assert due_universe_months(at("2026-10-01"), SEPT | {"2026-10"}) == ([], [])


# ── review of fixes 2026-09-28 ──────────────────────────────
from datetime import timedelta  # noqa: E402

from src.cli import is_universe_candidate, selection_gap  # noqa: E402
from src.core.config import load_settings  # noqa: E402


def test_stock_etf_commodity_perps_are_not_candidates():
    u = load_settings().universe
    assert is_universe_candidate("", "SOL", u)
    assert is_universe_candidate("innovation", "PUMPFUN", u)
    assert not is_universe_candidate("stock", "AAPL", u)
    assert not is_universe_candidate("ETF", "SOXL", u)
    assert not is_universe_candidate("commodity", "CL", u)
    assert not is_universe_candidate("", "XAU", u)  # second barrier: by base coin


SEP_LAST_BAR = datetime(2026, 9, 30, 20, tzinfo=UTC)


def test_selection_month_collected_to_the_end_passes():
    rows = {f"S{i}": SEP_LAST_BAR for i in range(100)}
    assert selection_gap("2026-09", rows) is None


def test_failed_candidates_backfill_is_caught_even_if_incumbents_are_fresh():
    """The night refreshes the current month's universe first, so the newest
    candle in the database does not reveal a failed collection of the rest."""
    rows = {f"IN{i}": SEP_LAST_BAR for i in range(27)}
    rows |= {f"OUT{i}": SEP_LAST_BAR - timedelta(days=6) for i in range(300)}
    gap = selection_gap("2026-09", rows)
    assert gap is not None and "27 of 327" in gap


def test_candidates_without_any_bars_count_as_stale_not_absent():
    """From November, candidates outside the universe have no selection-month
    candles at all until collected. A failed collection is None and must be seen."""
    rows = {f"IN{i}": SEP_LAST_BAR for i in range(30)}
    rows |= {f"OK{i}": SEP_LAST_BAR for i in range(40)}
    rows |= {f"MISS{i}": None for i in range(400)}
    gap = selection_gap("2026-09", rows)
    assert gap is not None and "70 of 470" in gap and "MISS" in gap


def test_too_few_expected_symbols_is_a_gap():
    assert "only 5" in selection_gap("2026-09", {f"S{i}": SEP_LAST_BAR for i in range(5)})
