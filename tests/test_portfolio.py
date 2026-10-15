"""Portfolio run. Spec §5.

This checks the step from "the signal has an edge" to "the account
captured it". Limits stand between them, and they cut out of the signal population
a subsample by features unrelated to signal quality.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.backtest.accounting import CloseReason, Trade, evaluate
from src.backtest.portfolio import replay
from src.core.models import RejectionCode
from src.risk.governor import GovernorLimits, TradingMode

D = Decimal
T0 = datetime(2026, 3, 2, tzinfo=UTC)  # Monday
H = timedelta(hours=1)

LIMITS = GovernorLimits(
    max_concurrent_positions=2,
    max_total_margin_pct=D("35"),
    max_correlated_positions=1,
    correlation_threshold=D("0.7"),
    daily_loss_limit_pct=D("10"),
    weekly_loss_limit_pct=D("20"),
)

#: MMR, lot step, minimum lot, minimum notional.
SPEC = (D("0.005"), D("0.001"), D("0.001"), D("5"))

SIZING = {
    "margin_pct": D("17.5"),
    "max_loss_pct": D("6"),
    "leverage_cap": D("10"),
    "min_liq_buffer_ratio": D("3"),
}


async def uncorrelated(_a, _b, _m):
    return D("0.1")


async def perfectly_correlated(_a, _b, _m):
    return D("0.95")


async def unknown(_a, _b, _m):
    return None


def outcome(symbol, *, opened, held_hours=4, r="1.0", is_long=True,
            entry="100", stop="97"):
    """A trade with a GIVEN result in R.

    R is set directly rather than derived from the exit price: the portfolio
    carries exactly it, and picking a price to get the desired R would
    test the arithmetic of `evaluate`, not the portfolio.
    """
    e, s = D(entry), D(stop)
    risk = abs(e - s)
    qty = D("1")
    exit_price = e + D(r) * risk if is_long else e - D(r) * risk
    trade = Trade(
        symbol=symbol,
        opened_at=opened,
        closed_at=opened + timedelta(hours=held_hours),
        is_long=is_long,
        entry=e,
        stop=s,
        exit_price=exit_price,
        qty=qty,
        notional=e * qty,
        margin=D("7"),
        close_reason=CloseReason.TARGET,
        target=exit_price,
    )
    return evaluate(trade)


async def run(per_symbol, *, correlation=uncorrelated, equity="40", limits=LIMITS):
    symbols = set(per_symbol)
    return await replay(
        per_symbol,
        limits=limits,
        instruments=dict.fromkeys(symbols, SPEC),
        initial_equity=D(equity),
        correlation=correlation,
        **SIZING,
    )


# ---------------------------------------------------------------------------
# Throughput
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_position_cap_cuts_the_signal_stream():
    """Four simultaneous signals with two slots: two are taken.

    Selection goes by arrival time, i.e. by a feature unrelated
    to signal quality.
    """
    per_symbol = {
        f"SYM{i}USDT": [outcome(f"SYM{i}USDT", opened=T0, held_hours=48)]
        for i in range(4)
    }
    result = await run(per_symbol)

    assert result.candidates == 4
    assert len(result.taken) == 2
    assert result.rejections[RejectionCode.MAX_POSITIONS.value] == 2
    assert result.taken_share == 0.5


@pytest.mark.asyncio
async def test_a_freed_slot_is_reused():
    per_symbol = {
        "AUSDT": [outcome("AUSDT", opened=T0, held_hours=2)],
        "BUSDT": [outcome("BUSDT", opened=T0, held_hours=2)],
        "CUSDT": [outcome("CUSDT", opened=T0 + H * 3, held_hours=2)],
    }
    result = await run(per_symbol)
    assert len(result.taken) == 3
    assert not result.rejections


@pytest.mark.asyncio
async def test_correlated_second_bet_is_refused():
    per_symbol = {
        "AUSDT": [outcome("AUSDT", opened=T0, held_hours=48)],
        "BUSDT": [outcome("BUSDT", opened=T0 + H, held_hours=48)],
    }
    result = await run(per_symbol, correlation=perfectly_correlated)
    assert len(result.taken) == 1
    assert result.rejections[RejectionCode.CORRELATION.value] == 1


@pytest.mark.asyncio
async def test_unknown_correlation_blocks_the_second_position():
    """P-001: bet independence is not confirmed, so we do not enter."""
    per_symbol = {
        "AUSDT": [outcome("AUSDT", opened=T0, held_hours=48)],
        "BUSDT": [outcome("BUSDT", opened=T0 + H, held_hours=48)],
    }
    result = await run(per_symbol, correlation=unknown)
    assert len(result.taken) == 1


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_equity_compounds_between_trades():
    """Size is computed on the ACTUAL equity at the entry moment.

    The per-symbol run sized every symbol from the same
    deposit: the sum of such trades describes several independent
    accounts, not one.
    """
    per_symbol = {
        "AUSDT": [
            outcome("AUSDT", opened=T0, held_hours=2, r="1.0"),
            outcome("AUSDT", opened=T0 + H * 5, held_hours=2, r="1.0"),
        ]
    }
    result = await run(per_symbol)
    assert len(result.taken) == 2
    assert result.equity > D("40")
    assert len(result.curve) == 2
    # The second trade is larger than the first: equity grew.
    first, second = result.curve
    assert second[1] - first[1] > first[1] - D("40")


@pytest.mark.asyncio
async def test_losses_reduce_equity_and_drawdown_is_tracked():
    per_symbol = {
        "AUSDT": [
            outcome("AUSDT", opened=T0, held_hours=2, r="-1.0"),
            outcome("AUSDT", opened=T0 + H * 5, held_hours=2, r="-1.0"),
        ]
    }
    result = await run(per_symbol)
    assert result.equity < D("40")
    assert result.max_drawdown_pct > 0
    assert result.total_return_pct < 0


@pytest.mark.asyncio
async def test_daily_loss_limit_stops_taking_signals():
    """A series of losses switches the account off, and further signals of the same day
    are not taken, however good they are."""
    trades = [
        outcome("AUSDT", opened=T0 + H * i * 2, held_hours=1, r="-1.0")
        for i in range(6)
    ]
    result = await run({"AUSDT": trades})

    assert result.rejections.get(RejectionCode.DAILY_STOP.value, 0) > 0
    assert len(result.taken) < len(trades)
    assert result.final_mode in {TradingMode.OFF_DAILY, TradingMode.OFF_WEEKLY}


@pytest.mark.asyncio
async def test_the_next_day_reopens_after_a_daily_stop():
    losers = [
        outcome("AUSDT", opened=T0 + H * i * 2, held_hours=1, r="-1.0")
        for i in range(5)
    ]
    tomorrow = [outcome("AUSDT", opened=T0 + timedelta(days=1), held_hours=1, r="1.0")]
    result = await run({"AUSDT": losers + tomorrow})

    taken_next_day = [
        o for o in result.taken if o.trade.opened_at >= T0 + timedelta(days=1)
    ]
    assert taken_next_day, "the daily ban did not lift when the UTC day changed"


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_input_is_not_an_error():
    result = await run({})
    assert result.candidates == 0
    assert result.equity == D("40")
    assert result.taken_share == 0.0


@pytest.mark.asyncio
async def test_unclosed_and_invalid_trades_are_not_candidates():
    """An unclosed position is not a trade with a zero result (P-001)."""
    open_trade = Trade(
        symbol="AUSDT", opened_at=T0, closed_at=None, is_long=True,
        entry=D("100"), stop=D("97"), exit_price=None, qty=D("1"),
        notional=D("100"), margin=D("7"), close_reason=CloseReason.DATA_END,
    )
    result = await run({"AUSDT": [evaluate(open_trade)]})
    assert result.candidates == 0


@pytest.mark.asyncio
async def test_missing_instrument_is_counted_not_silently_skipped():
    per_symbol = {"AUSDT": [outcome("AUSDT", opened=T0)]}
    result = await replay(
        per_symbol,
        limits=LIMITS,
        instruments={},  # no instrument parameters
        initial_equity=D("40"),
        correlation=uncorrelated,
        **SIZING,
    )
    assert not result.taken
    assert sum(result.rejections.values()) == 1


@pytest.mark.asyncio
async def test_candidates_are_processed_in_chronological_order():
    """The order of iterating symbols must not affect the result:
    otherwise the account would depend on dictionary ordering."""
    late = outcome("ZUSDT", opened=T0 + H * 10, held_hours=48)
    early = outcome("AUSDT", opened=T0, held_hours=48)
    mid = outcome("MUSDT", opened=T0 + H, held_hours=48)

    forward = await run({"AUSDT": [early], "MUSDT": [mid], "ZUSDT": [late]})
    backward = await run({"ZUSDT": [late], "MUSDT": [mid], "AUSDT": [early]})

    assert [o.trade.symbol for o in forward.taken] == ["AUSDT", "MUSDT"]
    assert [o.trade.symbol for o in forward.taken] == [
        o.trade.symbol for o in backward.taken
    ]
    assert forward.equity == backward.equity


# ---------------------------------------------------------------------------
# Simultaneous candidates (DEC-033)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_default_order_for_simultaneous_candidates_is_by_name():
    """This is how Setup C's numbers were computed, and that must not change retroactively."""
    per_symbol = {
        s: [outcome(s, opened=T0, held_hours=48)]
        for s in ("ZZZUSDT", "AAAUSDT", "MMMUSDT")
    }
    result = await run(per_symbol)
    assert [o.trade.symbol for o in result.taken] == ["AAAUSDT", "MMMUSDT"]


@pytest.mark.asyncio
async def test_seeded_tie_break_is_not_alphabetical_and_is_reproducible():
    """For D all signals arrive at the same daily close: ordering by name is
    a systematic alphabetical selection."""
    symbols = [f"S{i:02d}USDT" for i in range(20)]
    per_symbol = {s: [outcome(s, opened=T0, held_hours=48)] for s in symbols}

    async def taken(seed):
        result = await replay(
            per_symbol, limits=LIMITS, instruments=dict.fromkeys(symbols, SPEC),
            initial_equity=D("40"), correlation=uncorrelated,
            tie_break_seed=seed, **SIZING,
        )
        return [o.trade.symbol for o in result.taken]

    first = await taken(7)
    assert first == await taken(7), "one seed, one order"
    picks = {tuple(await taken(seed)) for seed in range(10)}
    assert len(picks) > 1, "different seeds must give different picks"
    assert first != sorted(symbols)[:2] or len(picks) > 1


@pytest.mark.asyncio
async def test_seeded_order_does_not_depend_on_input_order():
    symbols = [f"S{i:02d}USDT" for i in range(10)]
    forward = {s: [outcome(s, opened=T0, held_hours=48)] for s in symbols}
    backward = {s: forward[s] for s in reversed(symbols)}

    async def taken(book):
        result = await replay(
            book, limits=LIMITS, instruments=dict.fromkeys(symbols, SPEC),
            initial_equity=D("40"), correlation=uncorrelated,
            tie_break_seed=3, **SIZING,
        )
        return [o.trade.symbol for o in result.taken]

    assert await taken(forward) == await taken(backward)
