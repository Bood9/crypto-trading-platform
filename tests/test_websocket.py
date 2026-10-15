"""WS collector tests: message parsing and order book state.

No network is brought up: the pure processing logic is tested, and messages
are taken in the form Bybit v5 sends them.

Both datasets are for research and do not affect trading logic
(DEC-001, DEC-007), but writing garbage into them is still not allowed:
half a year later it would be taken as fact.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.collectors.websocket import (
    ORDERBOOK_DEPTH,
    WebSocketCollector,
    apply_orderbook_delta,
    build_snapshot,
    parse_liquidation,
)

D = Decimal
T0 = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Liquidations
# ---------------------------------------------------------------------------
def test_parses_liquidation_event():
    event = {"T": 1786104441624, "s": "SOLUSDT", "S": "Buy", "v": "125.4", "p": "73.82"}
    row = parse_liquidation(event, symbol="SOLUSDT")

    assert row is not None
    assert row["symbol"] == "SOLUSDT"
    assert row["qty"] == D("125.4")
    assert row["price"] == D("73.82")
    assert row["ts"].year == 2026


def test_malformed_liquidation_is_dropped_not_guessed():
    """P-001: losing a research dataset record is better than writing
    garbage that will later be taken as fact."""
    assert parse_liquidation({"s": "SOLUSDT"}, symbol="SOLUSDT") is None
    assert parse_liquidation({"T": "not a number"}, symbol="SOLUSDT") is None


def test_liquidation_falls_back_to_topic_symbol():
    event = {"T": 1786104441624, "S": "Sell", "v": "1", "p": "2"}
    row = parse_liquidation(event, symbol="ETHUSDT")
    assert row is not None
    assert row["symbol"] == "ETHUSDT"


# ---------------------------------------------------------------------------
# Order book state
# ---------------------------------------------------------------------------
def test_snapshot_replaces_whole_book():
    book = {"b": [["10", "1"]], "a": [["11", "1"]]}
    data = {"b": [["20", "2"]], "a": [["21", "2"]]}

    result = apply_orderbook_delta(book, data, "snapshot")

    assert result["b"] == [["20", "2"]]
    assert result["a"] == [["21", "2"]]


def test_delta_updates_existing_level():
    book = {"b": [["100", "5"]], "a": [["101", "5"]]}
    result = apply_orderbook_delta(book, {"b": [["100", "9"]]}, "delta")
    assert result["b"] == [["100", "9"]]


def test_zero_size_removes_level():
    book = {"b": [["100", "5"], ["99", "3"]], "a": [["101", "5"]]}
    result = apply_orderbook_delta(book, {"b": [["100", "0"]]}, "delta")
    assert result["b"] == [["99", "3"]]


def test_bids_stay_sorted_descending_and_asks_ascending():
    """Otherwise the "best" level would not be first, and the spread would be wrong."""
    book = {"b": [["100", "1"]], "a": [["101", "1"]]}
    result = apply_orderbook_delta(
        book, {"b": [["102", "1"], ["98", "1"]], "a": [["99", "1"], ["105", "1"]]}, "delta"
    )

    assert [lvl[0] for lvl in result["b"]] == ["102", "100", "98"]
    assert [lvl[0] for lvl in result["a"]] == ["99", "101", "105"]


def test_delta_on_empty_book_is_treated_as_snapshot():
    """Before the first snapshot there is nothing to apply changes to."""
    result = apply_orderbook_delta({}, {"b": [["10", "1"]], "a": [["11", "1"]]}, "delta")
    assert result["b"] == [["10", "1"]]


# ---------------------------------------------------------------------------
# Building a snapshot
# ---------------------------------------------------------------------------
def test_snapshot_computes_spread_and_depth():
    book = {
        "b": [[str(100 - i), "2"] for i in range(25)],
        "a": [[str(101 + i), "3"] for i in range(25)],
    }
    row = build_snapshot("SOLUSDT", book, T0)

    assert row is not None
    assert row["best_bid"] == D("100")
    assert row["best_ask"] == D("101")
    # Spread from the mid: 1 / 100.5 * 100.
    assert row["spread_pct"] == pytest.approx(D("0.99502"), abs=D("0.0001"))
    # Depth of 5 bid levels: 100*2 + 99*2 + 98*2 + 97*2 + 96*2.
    assert row["bid_depth_5_usd"] == D("980")
    assert row["bid_depth_20_usd"] > row["bid_depth_5_usd"]


def test_one_sided_book_yields_no_snapshot():
    """The spread is undefined; substituting zero is not allowed, since it
    reads as "no spread"."""
    assert build_snapshot("X", {"b": [["10", "1"]], "a": []}, T0) is None
    assert build_snapshot("X", {"b": [], "a": [["11", "1"]]}, T0) is None


def test_crossed_book_yields_no_snapshot():
    """An ask below the bid is a corrupted state, not a negative spread."""
    assert build_snapshot("X", {"b": [["11", "1"]], "a": [["10", "1"]]}, T0) is None


def test_zero_prices_yield_no_snapshot():
    assert build_snapshot("X", {"b": [["0", "1"]], "a": [["0", "1"]]}, T0) is None


def test_depth_ignores_malformed_levels():
    book = {"b": [["100", "1"], ["99"], ["98", "1"]], "a": [["101", "1"]]}
    row = build_snapshot("X", book, T0)
    assert row is not None
    assert row["bid_depth_5_usd"] == D("198")


# ---------------------------------------------------------------------------
# The collector as a whole
# ---------------------------------------------------------------------------
def test_topics_cover_both_streams():
    collector = WebSocketCollector(["SOLUSDT", "ETHUSDT"])
    topics = collector.topics()

    assert "allLiquidation.SOLUSDT" in topics
    assert f"orderbook.{ORDERBOOK_DEPTH}.ETHUSDT" in topics
    assert len(topics) == 4


def test_orderbook_can_be_disabled():
    collector = WebSocketCollector(["SOLUSDT"], collect_orderbook=False)
    assert collector.topics() == ["allLiquidation.SOLUSDT"]


def test_liquidation_message_is_queued():
    collector = WebSocketCollector(["SOLUSDT"])
    collector.handle_message(
        {
            "topic": "allLiquidation.SOLUSDT",
            "data": [{"T": 1786104441624, "s": "SOLUSDT", "S": "Buy", "v": "1", "p": "73"}],
        }
    )
    assert len(collector._pending_liquidations) == 1


def test_single_object_payload_is_accepted():
    """Bybit sends both a list and a single object."""
    collector = WebSocketCollector(["SOLUSDT"])
    collector.handle_message(
        {
            "topic": "allLiquidation.SOLUSDT",
            "data": {"T": 1786104441624, "s": "SOLUSDT", "S": "Buy", "v": "1", "p": "73"},
        }
    )
    assert len(collector._pending_liquidations) == 1


def test_snapshot_is_throttled_to_the_interval():
    """The delta stream runs at dozens of messages per second.

    There is no point storing all of them: we care about the spread
    distribution, not the microstructure.
    """
    collector = WebSocketCollector(["SOLUSDT"], snapshot_interval_s=60)
    message = {
        "topic": f"orderbook.{ORDERBOOK_DEPTH}.SOLUSDT",
        "type": "snapshot",
        "data": {"b": [["100", "1"]], "a": [["101", "1"]]},
    }

    collector.handle_message(message, now=T0)
    for i in range(1, 10):
        collector.handle_message(message, now=T0 + timedelta(seconds=i))

    assert len(collector._pending_snapshots) == 1

    collector.handle_message(message, now=T0 + timedelta(seconds=61))
    assert len(collector._pending_snapshots) == 2


def test_unknown_topic_is_ignored_without_crashing():
    collector = WebSocketCollector(["SOLUSDT"])
    collector.handle_message({"topic": "kline.1.SOLUSDT", "data": {}})
    assert collector._pending_liquidations == []
    assert collector._pending_snapshots == []


def test_snapshot_not_emitted_while_book_is_one_sided():
    collector = WebSocketCollector(["SOLUSDT"], snapshot_interval_s=0)
    collector.handle_message(
        {
            "topic": f"orderbook.{ORDERBOOK_DEPTH}.SOLUSDT",
            "type": "snapshot",
            "data": {"b": [["100", "1"]], "a": []},
        },
        now=T0,
    )
    assert collector._pending_snapshots == []
