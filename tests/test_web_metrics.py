"""Panel numbers and the run journal. DEC-041.

The panel shows money, so every number on it is computed on the server
and tested here, not in the browser.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from src.live.cycle import CycleReport
from src.live.report import failure, render
from src.live.signals import Skip
from src.live.store import LiveTrade
from src.risk.governor import TradingMode
from src.web import metrics

D = Decimal
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def trade(pnl: str | None, *, day: int = 1, long: bool = True, status: str = "closed",
          entry: str = "100", stop: str = "90", qty: str = "0.15") -> LiveTrade:
    return LiveTrade(
        setup="b", symbol=f"S{day}USDT", is_long=long, qty=D(qty), entry_price=D(entry),
        stop_price=D(stop), margin=D(2), entry_link_id=f"e{day}", stop_link_id=f"s{day}",
        bar_ts=T0, opened_at=T0 + timedelta(days=day - 1),
        close_due_at=T0 + timedelta(days=day + 6), status=status,
        closed_at=T0 + timedelta(days=day) if status == "closed" else None,
        pnl=None if pnl is None else D(pnl), id=day,
    )


def test_r_is_result_over_the_risk_at_the_stop():
    # Risk: 10 × 0.15 = 1.5 $. A loss of 1.5 $ is exactly −1R.
    assert metrics.r_multiple(trade("-1.5")) == D(-1)
    assert metrics.r_multiple(trade("3")) == D(2)
    assert metrics.r_multiple(trade(None, status="open")) is None


def test_curve_starts_at_the_deposit_and_steps_at_each_close():
    trades = [trade("2", day=1), trade("-1", day=3), trade(None, day=4, status="open")]
    curve = metrics.equity_curve(trades, D(100), T0)
    assert [v for _, v in curve] == [D(100), D(102), D(101)]
    assert curve[0][0] == T0


def test_drawdown_is_from_the_peak_not_from_the_start():
    trades = [trade("10", day=1), trade("-11", day=2)]
    st = metrics.stats(trades, D(100), T0)
    assert st.max_drawdown_pct == D(10)  # 110 → 99
    assert st.closed == 2 and st.wins == 1 and st.win_rate == D(50)
    assert st.total_pnl == D(-1)


def test_empty_book_has_no_invented_numbers():
    st = metrics.stats([], D(100), None)
    assert (st.closed, st.avg_r, st.best, st.win_rate) == (0, None, None, None)
    assert metrics.equity_curve([], D(100), None) == []


def test_unrealized_and_stop_distance_by_side():
    long = trade(None, status="open")
    short = trade(None, status="open", long=False, stop="110")
    assert metrics.unrealized(long, D(104)) == D("0.6")
    assert metrics.unrealized(short, D(104)) == D("-0.6")
    assert metrics.stop_distance_pct(long, D(100)) == D(10)
    # A short with price above the stop has passed the stop: negative distance.
    assert metrics.stop_distance_pct(short, D(125)) < 0


def test_run_block_says_what_happened_in_plain_words():
    report = CycleReport(moment=datetime(2026, 9, 26, tzinfo=UTC), dry_run=False)
    report.equity, report.mode = D("98.5"), TradingMode.ON
    report.closed = [("SOLUSDT", "STOP", D("-1.52"))]
    report.opened = ["TAOUSDT"]
    report.skipped = [("A", "MAX_POSITIONS"), ("B", "MAX_POSITIONS"), ("C", "PRICE_DIVERGENCE")]
    text = render(
        book="setup_e:paper", now=datetime(2026, 9, 26, 0, 10, tzinfo=UTC), execute=True,
        universe=27, signals=5, skips={"X": Skip.STALE_DATA, "Y": Skip.NO_SIGNAL},
        report=report, events=["SOLUSDT stop 90 filled at 89"],
    )
    assert "03:10 MSK" in text
    assert "closed 1 (-1.52 $)" in text and "opened: TAOUSDT" in text
    assert "SOLUSDT by stop: -1.52 $" in text
    assert "stale data** (1): X" in text
    assert "all slots taken — 2" in text
    assert "Y" not in text.split("stale data")[1].split("\n")[0]


def test_halt_is_the_first_thing_in_the_block():
    report = CycleReport(moment=datetime(2026, 9, 26, tzinfo=UTC), dry_run=False, halted=True)
    report.equity, report.mode = D("100"), TradingMode.OFF_MANUAL
    text = render(book="b", now=datetime(2026, 9, 26, 0, 10, tzinfo=UTC), execute=True,
                  universe=1, signals=0, skips={}, report=report, events=[])
    assert "**Summary:** **TRADING DISABLED" in text


def test_failure_block_names_the_reason():
    text = failure(
        book="b", now=datetime(2026, 9, 26, 1, 19, tzinfo=UTC), reason="database unavailable"
    )
    assert "NOT EXECUTED" in text and "database unavailable" in text and "04:19 MSK" in text
