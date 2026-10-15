"""Tests for stream start dates and effective_start.

Key point: for some pairs OI starts EARLIER than candles (ETHUSDT: OI
since 2020-10, candles since 2021-03). A single listing date would be wrong
for at least one stream, so the boundaries are stored separately.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.collectors.stream_starts import SymbolStarts, assess_readiness
from src.core.models import DataStream

SETUP_B = frozenset(
    {DataStream.KLINE_4H, DataStream.KLINE_1H, DataStream.OI, DataStream.FUNDING}
)


def d(y, m, day=1) -> datetime:
    return datetime(y, m, day, tzinfo=UTC)


def starts(**kw) -> SymbolStarts:
    mapping = {DataStream(k): v for k, v in kw.items()}
    return SymbolStarts(
        symbol=kw.pop("symbol", "TESTUSDT") if "symbol" in kw else "TESTUSDT",
        starts={s: mapping.get(s) for s in DataStream},
        ends={s: d(2026, 8) for s in DataStream},
    )


def test_effective_start_is_the_latest_stream():
    """A setup can start working only when EVERYTHING is available."""
    item = SymbolStarts(
        symbol="ETHUSDT",
        starts={
            DataStream.KLINE_15M: d(2021, 3),
            DataStream.KLINE_1H: d(2021, 3),
            DataStream.KLINE_4H: d(2021, 3),
            # OI starts earlier than candles: the real ETHUSDT case.
            DataStream.OI: d(2020, 10),
            DataStream.FUNDING: d(2020, 10),
        },
        ends={},
    )
    assert item.effective_for(SETUP_B) == d(2021, 3)


def test_missing_stream_yields_no_effective_start():
    """P-001: a missing stream is not replaced with a neighbour's date."""
    item = SymbolStarts(
        symbol="MULTIUSDT",
        starts={
            DataStream.KLINE_15M: d(2022, 1),
            DataStream.KLINE_1H: d(2022, 1),
            DataStream.KLINE_4H: d(2022, 1),
            DataStream.OI: None,
            DataStream.FUNDING: None,
        },
        ends={},
    )
    assert item.effective_for(SETUP_B) is None
    assert item.missing(SETUP_B) == [DataStream.FUNDING, DataStream.OI]


def test_readiness_separates_no_data_from_missing_stream():
    """Two different problems that would otherwise merge into "unfit".

    The symbol was not downloaded at all: that is a collection gap. The symbol lacks
    a specific stream: that is a fact about the data (MULTIUSDT).
    """
    data = {
        "GOODUSDT": SymbolStarts(
            "GOODUSDT", {s: d(2024, 8) for s in DataStream}, {}
        ),
        "MULTIUSDT": SymbolStarts(
            "MULTIUSDT",
            {
                DataStream.KLINE_15M: d(2022, 1),
                DataStream.KLINE_1H: d(2022, 1),
                DataStream.KLINE_4H: d(2022, 1),
                DataStream.OI: None,
                DataStream.FUNDING: None,
            },
            {},
        ),
        "NOTHINGUSDT": SymbolStarts("NOTHINGUSDT", {s: None for s in DataStream}, {}),
    }

    readiness = assess_readiness(data, "setup_b", SETUP_B)

    assert [s for s, _ in readiness.ready] == ["GOODUSDT"]
    assert list(readiness.missing_streams) == ["MULTIUSDT"]
    assert readiness.no_data == ["NOTHINGUSDT"]
    assert readiness.total == 3


def test_readiness_summary_adds_up():
    data = {
        f"S{i}USDT": SymbolStarts(f"S{i}USDT", {s: d(2024, 8) for s in DataStream}, {})
        for i in range(5)
    }
    readiness = assess_readiness(data, "setup_b", SETUP_B)
    assert sum(readiness.summary().values()) == readiness.total == 5


def test_empty_universe_is_handled():
    readiness = assess_readiness({}, "setup_b", SETUP_B)
    assert readiness.total == 0
    assert all(v == 0 for v in readiness.summary().values())


# ---------------------------------------------------------------------------
# Loss due to a single stream
# ---------------------------------------------------------------------------
def full(symbol: str) -> SymbolStarts:
    return SymbolStarts(symbol, {s: d(2024, 8) for s in DataStream}, {})


def without(symbol: str, *absent: DataStream) -> SymbolStarts:
    return SymbolStarts(
        symbol,
        {s: (None if s in absent else d(2024, 8)) for s in DataStream},
        {},
    )


def test_single_missing_stream_is_counted_separately():
    """A symbol with full candles but no OI is lost entirely.

    The total number of unfit symbols does not show this: it also holds symbols
    that have nothing. The difference matters: if the universe melts because of
    one stream, that is cured by a backfill, not by acceptance.
    """
    data = {
        "A": full("A"),
        "NOOI": without("NOOI", DataStream.OI),
        "NOFUND": without("NOFUND", DataStream.FUNDING),
        "NOBOTH": without("NOBOTH", DataStream.OI, DataStream.FUNDING),
    }
    readiness = assess_readiness(data, "setup_b", SETUP_B)

    lost = readiness.lost_to_single_stream
    assert set(lost) == {"NOOI", "NOFUND"}
    assert "NOBOTH" not in lost, "two streams missing is a different case"
    assert readiness.single_stream_breakdown() == {
        DataStream.OI: 1,
        DataStream.FUNDING: 1,
    }


def test_breakdown_shows_which_stream_hurts_most():
    data = {f"S{i}": without(f"S{i}", DataStream.OI) for i in range(5)}
    data["F"] = without("F", DataStream.FUNDING)

    breakdown = assess_readiness(data, "setup_b", SETUP_B).single_stream_breakdown()

    assert next(iter(breakdown)) is DataStream.OI
    assert breakdown[DataStream.OI] == 5


# ---------------------------------------------------------------------------
# Formally present, actually full of gaps
# ---------------------------------------------------------------------------
def test_sparse_coverage_disqualifies_despite_stream_being_present():
    """Completeness by presence and series density are different things.

    A series with 40% of bars formally "exists", but is fit neither for ATR
    nor for a breakout.

    IMPORTANT: density is computed within the series' OWN range,
    not over the period. Otherwise the check would confuse a gappy series with a young
    pair: the first version did exactly that and would have rejected 49 healthy
    symbols.
    """
    data = {"THINUSDT": full("THINUSDT"), "GOODUSDT": full("GOODUSDT")}
    coverage = {(s, st): 0.99 for s in data for st in DataStream}
    coverage[("THINUSDT", DataStream.OI)] = 0.40

    readiness = assess_readiness(data, "setup_b", SETUP_B, coverage)

    assert [s for s, _ in readiness.ready] == ["GOODUSDT"]
    assert "THINUSDT" in readiness.too_sparse
    assert readiness.too_sparse["THINUSDT"][0][0] is DataStream.OI


def test_coverage_check_is_skipped_when_not_supplied():
    """Without coverage data the check does not make things up."""
    data = {"A": full("A")}
    readiness = assess_readiness(data, "setup_b", SETUP_B, coverage=None)
    assert len(readiness.ready) == 1
    assert readiness.too_sparse == {}


def test_missing_coverage_entry_counts_as_zero():
    """P-001: a missing value is not counted as good."""
    data = {"A": full("A")}
    readiness = assess_readiness(data, "setup_b", SETUP_B, coverage={})
    assert "A" in readiness.too_sparse


def test_total_accounts_for_every_category():
    data = {
        "READY": full("READY"),
        "NOOI": without("NOOI", DataStream.OI),
        "NOTHING": SymbolStarts("NOTHING", {s: None for s in DataStream}, {}),
        "THIN": full("THIN"),
    }
    coverage = {(s, st): 0.99 for s in data for st in DataStream}
    coverage[("THIN", DataStream.KLINE_1H)] = 0.1

    readiness = assess_readiness(data, "setup_b", SETUP_B, coverage)

    assert readiness.total == 4, "a symbol got lost between categories"


def test_young_symbol_is_not_mistaken_for_patchy_data():
    """A young pair with a dense series is not gappy data.

    A measurement on collected data: 50 symbols had period coverage
    below 80% with 100% density of their own series. A check
    computing the share of the period would have rejected them all, and that is
    a normal case handled by the short-symbol policy.
    """
    data = {"YOUNGUSDT": full("YOUNGUSDT")}
    # The density of its own range is high even though the history is short.
    coverage = {("YOUNGUSDT", st): 1.0 for st in DataStream}

    readiness = assess_readiness(data, "setup_b", SETUP_B, coverage)

    assert [s for s, _ in readiness.ready] == ["YOUNGUSDT"]
    assert readiness.too_sparse == {}
