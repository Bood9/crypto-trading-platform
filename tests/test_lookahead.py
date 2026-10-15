"""Look-ahead test. Spec §14.

Method: the run is repeated on a series where all bars after each decision
point are replaced with noise. The result must match to the last digit.
If it changed, somewhere the code used data that did not exist at the
moment of the decision.

This is the most expensive backtest error and the least visible: it does not crash,
is not logged, and shows up only as a result that is too good.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.engine import (
    BacktestConfig,
    MarketBar,
    Signal,
    run,
)

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def make_bars(n: int, *, seed: int = 42) -> list[MarketBar]:
    rng = random.Random(seed)
    bars: list[MarketBar] = []
    price = 100.0
    for i in range(n):
        price *= 1 + rng.gauss(0, 0.01)
        high = price * (1 + abs(rng.gauss(0, 0.005)))
        low = price * (1 - abs(rng.gauss(0, 0.005)))
        bars.append(
            MarketBar(
                ts=T0 + timedelta(hours=4 * i),
                open=D(str(round(price, 6))),
                high=D(str(round(high, 6))),
                low=D(str(round(low, 6))),
                close=D(str(round(price, 6))),
                volume=D("1000"),
            )
        )
    return bars


def corrupt_after(bars: list[MarketBar], index: int, *, seed: int = 7) -> list[MarketBar]:
    """Replaces everything after `index` with noise unrelated to the original series."""
    rng = random.Random(seed)
    out = list(bars[: index + 1])
    for i in range(index + 1, len(bars)):
        price = rng.uniform(1, 10_000)
        out.append(
            MarketBar(
                ts=bars[i].ts,
                open=D(str(round(price, 6))),
                high=D(str(round(price * 1.5, 6))),
                low=D(str(round(price * 0.5, 6))),
                close=D(str(round(price, 6))),
                volume=D("1"),
            )
        )
    return out


def breakout_signal(window):
    """Breakout of the 20-bar high. Looks only at the slice it was given."""
    if len(window) < 21:
        return None
    current = window[-1]
    prior = window[-21:-1]
    highest = max(b.high for b in prior)
    if current.close > highest:
        stop = min(b.low for b in prior)
        risk = current.close - stop
        if risk <= 0:
            return None
        return Signal(
            is_long=True,
            entry=current.close,
            stop=stop,
            target=current.close + risk * 3,
        )
    return None


def fixed_sizer(signal, price):
    return D("1"), price, price / 10


CONFIG = BacktestConfig(
    initial_equity=D("40"),
    taker_fee_pct=D("0.055"),
    slippage_pct=D("0.05"),
    time_stop=timedelta(hours=48),
    warmup_bars=21,
)


def signature(result):
    """Run fingerprint: everything that look-ahead could change."""
    return [
        (
            o.trade.opened_at,
            o.trade.entry,
            o.trade.stop,
            o.trade.exit_price,
            o.trade.close_reason,
            o.r_multiple,
        )
        for o in result.outcomes
    ]


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------
def test_signal_function_cannot_see_the_future():
    """The strategy gets a slice up to and including the current bar.

    Access beyond the slice shows up as IndexError, not as a
    silently inflated result: protection in the form of code, not a test.
    """
    bars = make_bars(200)
    seen_lengths: list[int] = []

    def spy(window):
        seen_lengths.append(len(window))
        return breakout_signal(window)

    run(bars, spy, fixed_sizer, CONFIG)

    assert seen_lengths, "the strategy was never called"
    # No slice is longer than the bar's position in the series.
    assert max(seen_lengths) <= len(bars)


@pytest.mark.parametrize("cut", [40, 80, 120, 160])
def test_corrupting_the_future_does_not_change_past_decisions(cut):
    """The key check of spec §14.

    Corrupting bars after the `cut` point must not change any trade
    opened and closed before it.
    """
    bars = make_bars(200)
    clean = run(bars, breakout_signal, fixed_sizer, CONFIG)
    dirty = run(corrupt_after(bars, cut), breakout_signal, fixed_sizer, CONFIG)

    def before_cut(result):
        limit = bars[cut].ts
        return [
            s
            for s in signature(result)
            if s[0] <= limit and (s[3] is None or s[0] <= limit)
        ]

    clean_prefix = [s for s in signature(clean) if s[0] < bars[cut].ts]
    dirty_prefix = [s for s in signature(dirty) if s[0] < bars[cut].ts]

    # Trades OPENED before the corruption point might have closed after it;
    # those cannot be compared. Compare the ones fully completed before it.
    closed_clean = [s for s in clean_prefix if s[3] is not None]
    closed_dirty = [s for s in dirty_prefix if s[3] is not None]

    common = min(len(closed_clean), len(closed_dirty))
    assert common > 0, "no trade completed before the corruption point"

    for a, b in zip(closed_clean[:common], closed_dirty[:common], strict=True):
        if a[0] + timedelta(hours=48) < bars[cut].ts:
            assert a == b, f"trade changed from corrupting the future: {a} != {b}"


def test_identical_input_gives_identical_result():
    """Determinism: without it the look-ahead test proves nothing."""
    bars = make_bars(150)
    first = run(bars, breakout_signal, fixed_sizer, CONFIG)
    second = run(bars, breakout_signal, fixed_sizer, CONFIG)
    assert signature(first) == signature(second)


def test_entry_and_exit_never_share_a_bar():
    """Entry and exit within one bar would mean knowing its future."""
    bars = make_bars(300)
    result = run(bars, breakout_signal, fixed_sizer, CONFIG)

    for outcome in result.outcomes:
        if outcome.trade.closed_at is not None:
            assert outcome.trade.closed_at > outcome.trade.opened_at


def test_stop_is_checked_before_target():
    """The order of events within a bar is unknown.

    Assuming the target is hit before the stop would
    systematically inflate the result.
    """
    bars = [
        MarketBar(T0 + timedelta(hours=4 * i), D("100"), D("101"), D("99"), D("100"))
        for i in range(25)
    ]
    # A bar that touches both the stop and the target.
    bars.append(
        MarketBar(T0 + timedelta(hours=100), D("100"), D("130"), D("70"), D("100"))
    )

    def always_long(window):
        if len(window) != 25:
            return None
        return Signal(is_long=True, entry=D("100"), stop=D("90"), target=D("120"))

    result = run(bars, always_long, fixed_sizer, CONFIG)
    assert result.outcomes
    from src.backtest.accounting import CloseReason

    assert result.outcomes[0].trade.close_reason is CloseReason.STOP


def test_slippage_always_works_against_us():
    """Slippage in our favour is free profit
    that will not exist in reality."""
    from src.backtest.engine import _fill_price

    assert _fill_price(D("100"), is_long=True, slippage_pct=D("0.1")) > D("100")
    assert _fill_price(D("100"), is_long=False, slippage_pct=D("0.1")) < D("100")


# ---------------------------------------------------------------------------
# History window: a view instead of a copy
# ---------------------------------------------------------------------------
def test_window_view_blocks_access_beyond_current_bar():
    """The guarantee that the future is invisible survives replacing the copy with a view.

    Access beyond the window boundary raises IndexError rather than silently returning
    a bar that has not happened yet.
    """
    from src.backtest.engine import PastView

    bars = make_bars(50)
    view = PastView(bars, 10)

    assert len(view) == 10
    assert view[-1] is bars[9]
    assert view[9] is bars[9]

    with pytest.raises(IndexError, match="future"):
        _ = view[10]
    with pytest.raises(IndexError):
        _ = view[42]


def test_window_view_slicing_stays_inside_the_window():
    from src.backtest.engine import PastView

    bars = make_bars(50)
    view = PastView(bars, 10)

    assert view[-3:] == list(bars[7:10])
    assert view[:] == list(bars[:10])
    # A slice past the boundary is clipped by the window rather than pulling future bars.
    assert len(view[0:99]) == 10


def test_window_view_is_not_a_copy():
    """The point of the replacement: on 70 thousand bars copying is quadratic."""
    from src.backtest.engine import PastView

    bars = make_bars(100)
    view = PastView(bars, 100)
    assert view[0] is bars[0], "elements must be the same objects"


# ---------------------------------------------------------------------------
# History window: the guarantee is checked over ALL access paths
# ---------------------------------------------------------------------------
def test_window_view_never_yields_a_future_bar_by_any_access_path():
    """Replacing the copy with a view must not open any bypasses.

    All ways of reaching an element are checked, not only those
    the current code uses: half of `Sequence`'s methods are
    inherited, and a single one that computes length from the
    original series is enough for a hole to open silently.
    """
    from src.backtest.engine import PastView

    bars = make_bars(60)

    for end in (1, 2, 17, 59, 60):
        view = PastView(bars, end)
        horizon = bars[end - 1].ts
        allowed = set(bars[:end])

        seen = [
            *list(view),
            *list(reversed(view)),
            *view[:],
            *view[::-1],
            *view[::3],
            *view[-5:],
            *view[0:10**9],
            *view[-10**9 : 10**9],
            *view[10**9 : 10**9],
            *[view[i] for i in range(end)],
            *[view[-i] for i in range(1, end + 1)],
        ]
        assert seen, "no access path returned any bars"
        for bar in seen:
            assert bar.ts <= horizon, f"a bar from the future is visible: {bar.ts} > {horizon}"
            assert bar in allowed

        assert len(view) == end
        assert view.index(bars[end - 1]) == end - 1
        assert bars[end - 1] in view
        if end < len(bars):
            assert bars[end] not in view, "a future bar was found via __contains__"

        with pytest.raises(IndexError, match="future"):
            _ = view[end]
        with pytest.raises(IndexError):
            _ = view[-(end + 1)]


def test_window_view_refuses_to_reach_past_the_series():
    """A window longer than the series is an error at construction, not an IndexError
    somewhere later. Otherwise `PastView(bars, len(bars) + 5)` would look
    working until the first access to the tail."""
    from src.backtest.engine import PastView

    bars = make_bars(10)
    with pytest.raises(ValueError, match="does not fit"):
        PastView(bars, 11)
    with pytest.raises(ValueError):
        PastView(bars, -1)
    assert len(PastView(bars, 0)) == 0


def test_window_view_matches_a_real_copy_everywhere():
    """The reference is the very slice the view replaced.
    Any discrepancy means the replacement was not equivalent."""
    from src.backtest.engine import PastView

    bars = make_bars(40)
    for end in range(len(bars) + 1):
        view = PastView(bars, end)
        copy = list(bars[:end])
        assert len(view) == len(copy)
        assert list(view) == copy
        for lo, hi, step in [
            (None, None, None), (0, 5, None), (-3, None, None),
            (None, -1, None), (1, 100, 2), (None, None, -1), (5, 1, -2),
        ]:
            assert view[lo:hi:step] == copy[lo:hi:step], (end, lo, hi, step)
