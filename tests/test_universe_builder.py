"""Tests for building the universe from data.

The main thing checked here is the absence of lookahead: the universe
for month M must be computed from the turnover of month M−lag and never
from the turnover of the period being traded (DEC-003, condition 1).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.config import load_settings
from src.core.models import DataStream, RejectionCode
from src.strategy.universe import (
    SymbolMonthStats,
    UniverseEntry,
    build_universe,
    previous_month,
)
from src.strategy.universe_builder import UniverseBuildResult

D = Decimal


@pytest.fixture
def config():
    return load_settings().universe


def stat(symbol: str, turnover: str, *, bars: int = 180, listing=None, streams=frozenset()):
    from datetime import UTC, datetime

    return SymbolMonthStats(
        symbol=symbol,
        turnover=D(turnover),
        bar_count=bars,
        listing_date=listing or datetime(2020, 1, 1, tzinfo=UTC),
        available_streams=streams,
    )


# ---------------------------------------------------------------------------
# Selection lag
# ---------------------------------------------------------------------------
def test_previous_month_crosses_year_boundary():
    assert previous_month("2026-01", 1) == "2025-12"
    assert previous_month("2026-03", 3) == "2025-12"


def test_lag_is_at_least_one_month(config):
    """Selecting by the traded month's turnover would be lookahead."""
    assert config.selection_lag_months >= 1


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def test_top_n_is_respected(config):
    stats = [stat(f"S{i}USDT", str(10_000_000_000 - i)) for i in range(50)]
    entries = build_universe("2026-03", stats, config)

    included = [e for e in entries if e.included]
    assert len(included) == config.top_n


def test_ranking_is_by_turnover(config):
    stats = [stat("LOW", "3000000000"), stat("HIGH", "9000000000")]
    entries = build_universe("2026-03", stats, config)

    by_symbol = {e.symbol: e for e in entries}
    assert by_symbol["HIGH"].rank < by_symbol["LOW"].rank


def test_liquidity_floor_rejects_thin_symbol(config):
    """An absolute floor, not just a rank: in a sluggish period the top 30
    may include a pair on which the stop would slip."""
    thin = stat("THINUSDT", "100000")  # ~3k/day
    entries = build_universe("2026-03", [thin], config)

    assert entries[0].included is False
    assert entries[0].rejection is RejectionCode.LOW_LIQUIDITY


def test_young_symbol_is_rejected_before_liquidity(config):
    """The order of checks is part of the contract.

    A fresh coin with huge turnover must get TOO_YOUNG rather than pass
    on liquidity: its abnormal funding and OI spikes come from an immature
    market, and there is no history to check that against.
    """
    from datetime import UTC, datetime

    young = stat(
        "FRESHUSDT",
        "9000000000",
        listing=datetime(2026, 2, 20, tzinfo=UTC),
    )
    entries = build_universe("2026-03", [young], config)

    assert entries[0].rejection is RejectionCode.TOO_YOUNG


def test_unknown_listing_date_is_rejected(config):
    """P-001: an unknown age is no reason to let it through."""
    unknown = stat("DEADUSDT", "9000000000", listing=None)
    unknown = SymbolMonthStats(
        symbol="DEADUSDT",
        turnover=D("9000000000"),
        bar_count=180,
        listing_date=None,
    )
    entries = build_universe("2026-03", [unknown], config)
    assert entries[0].rejection is RejectionCode.TOO_YOUNG


def test_missing_stream_is_rejected(config):
    """The MULTIUSDT case: candles exist, OI and funding do not."""
    required = frozenset({DataStream.OI, DataStream.FUNDING})
    partial = stat(
        "MULTIUSDT",
        "9000000000",
        streams=frozenset({DataStream.KLINE_4H}),
    )
    entries = build_universe("2026-03", [partial], config, required_streams=required)

    assert entries[0].rejection is RejectionCode.MISSING_STREAM


def test_symbol_with_all_streams_passes(config):
    required = frozenset({DataStream.OI, DataStream.FUNDING})
    full = stat(
        "SOLUSDT",
        "9000000000",
        streams=frozenset({DataStream.OI, DataStream.FUNDING, DataStream.KLINE_4H}),
    )
    entries = build_universe("2026-03", [full], config, required_streams=required)

    assert entries[0].included
    assert entries[0].rejection is None


def test_every_candidate_gets_a_row(config):
    """Rejected ones are written too: the snapshot must explain why
    a symbol is not in the universe."""
    stats = [stat(f"S{i}USDT", str(9_000_000_000 - i)) for i in range(40)]
    entries = build_universe("2026-03", stats, config)
    assert len(entries) == 40


# ---------------------------------------------------------------------------
# Aggregate over the period
# ---------------------------------------------------------------------------
def make_entry(month: str, symbol: str, included: bool) -> UniverseEntry:
    return UniverseEntry(
        month=month,
        symbol=symbol,
        turnover_prev_period=D("1"),
        avg_daily_turnover=D("1"),
        rank=1,
        included=included,
    )


def test_selected_symbols_is_the_union_across_months():
    result = UniverseBuildResult(
        entries=[
            make_entry("2026-01", "A", True),
            make_entry("2026-02", "B", True),
            make_entry("2026-02", "C", False),
        ],
        months=["2026-01", "2026-02"],
    )
    assert result.selected_symbols() == ["A", "B"]


def test_months_by_symbol_counts_only_inclusions():
    result = UniverseBuildResult(
        entries=[
            make_entry("2026-01", "A", True),
            make_entry("2026-02", "A", True),
            make_entry("2026-03", "A", False),
        ],
        months=["2026-01", "2026-02", "2026-03"],
    )
    assert result.months_by_symbol() == {"A": 2}


def test_empty_result_is_handled():
    result = UniverseBuildResult(entries=[], months=[])
    assert result.selected_symbols() == []
    assert result.months_by_symbol() == {}


# ---------------------------------------------------------------------------
# Tradability
# ---------------------------------------------------------------------------
def test_untradable_symbol_is_rejected(config):
    """BTCUSDT passes on turnover and age, but its lot step equals the position.

    Without this check the backtest would trade a pair that we physically
    cannot trade.
    """
    entries = build_universe(
        "2026-03",
        [stat("BTCUSDT", "9000000000"), stat("SOLUSDT", "8000000000")],
        config,
        tradable=frozenset({"SOLUSDT"}),
    )
    by_symbol = {e.symbol: e for e in entries}

    assert by_symbol["BTCUSDT"].rejection is RejectionCode.NO_PARTIAL_EXIT
    assert by_symbol["SOLUSDT"].included


def test_tradability_is_checked_before_liquidity(config):
    """Order matters: an untradable symbol must get its own code, not
    LOW_LIQUIDITY — otherwise an investigation would chase the wrong problem."""
    entries = build_universe(
        "2026-03",
        [stat("BTCUSDT", "100")],  # both untradable and illiquid
        config,
        tradable=frozenset(),
    )
    assert entries[0].rejection is RejectionCode.NO_PARTIAL_EXIT


def test_no_tradable_list_means_no_check(config):
    """None means no list was passed, so no check is made."""
    entries = build_universe("2026-03", [stat("ANYUSDT", "9000000000")], config)
    assert entries[0].included


def test_empty_tradable_set_rejects_everyone(config):
    """An empty set is not "no check" (P-001)."""
    entries = build_universe(
        "2026-03", [stat("ANYUSDT", "9000000000")], config, tradable=frozenset()
    )
    assert not entries[0].included
