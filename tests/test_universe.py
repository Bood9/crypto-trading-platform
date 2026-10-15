"""Point-in-time universe tests (DEC-003).

The main one is on look-ahead: selection must go by month M−1 turnover,
never by the turnover of the month we trade in.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from src.core.config import load_settings
from src.core.models import DataStream, RejectionCode
from src.strategy.universe import (
    SymbolMonthStats,
    build_universe,
    effective_start,
    expected_bars,
    listing_age_days,
    month_bounds,
    month_key,
    months_between,
    previous_month,
    qualifies_for_fitting,
    qualifies_for_oos,
)

D = Decimal
ALL_STREAMS = frozenset(DataStream)


@pytest.fixture
def config():
    return load_settings().universe


# All tests build the universe for 2026-05 with lag=1, i.e. the selection month is
# April, and age is measured at its end. The anchor must match this
# point, otherwise the test checks something other than it thinks.
SELECTION_END = datetime(2026, 5, 1, tzinfo=UTC)


def stats(symbol, turnover_musd, *, age_days=400, streams=ALL_STREAMS):
    listing = SELECTION_END - timedelta(days=age_days)
    return SymbolMonthStats(
        symbol=symbol,
        turnover=D(str(turnover_musd)) * D(1_000_000),
        bar_count=744,
        listing_date=listing,
        available_streams=streams,
    )


# ---------------------------------------------------------------------------
# Month arithmetic
# ---------------------------------------------------------------------------
def test_previous_month_crosses_year_boundary():
    assert previous_month("2026-01") == "2025-12"
    assert previous_month("2026-03", lag=3) == "2025-12"


def test_previous_month_rejects_zero_lag():
    """lag=0 would mean selection by the current month, i.e. look-ahead."""
    with pytest.raises(ValueError):
        previous_month("2026-05", lag=0)


def test_month_bounds_are_half_open():
    start, end = month_bounds("2026-02")
    assert start == datetime(2026, 2, 1, tzinfo=UTC)
    assert end == datetime(2026, 3, 1, tzinfo=UTC)


def test_december_bounds_roll_into_next_year():
    _, end = month_bounds("2026-12")
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_months_between_is_inclusive():
    keys = months_between(datetime(2025, 11, 5, tzinfo=UTC), datetime(2026, 2, 3, tzinfo=UTC))
    assert keys == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_month_key_format():
    assert month_key(datetime(2026, 8, 7, tzinfo=UTC)) == "2026-08"


# ---------------------------------------------------------------------------
# Look-ahead
# ---------------------------------------------------------------------------
def test_selection_uses_lagged_month(config):
    """The snapshot is labelled with the target month; the data comes from the previous one."""
    entries = build_universe("2026-05", [stats("SOLUSDT", 3000)], config)

    assert entries[0].month == "2026-05"
    # The turnover in the record is the one passed in, i.e. for the selection month.
    assert entries[0].turnover_prev_period == D(3000) * D(1_000_000)


def test_ranking_ignores_future_performance(config):
    """A coin that took off in the traded month must not get in retroactively.

    Selection sees only the turnover of the selection month: if at that time the coin
    was thin, it is rejected regardless of what happened later.
    """
    quiet_then_exploded = stats("LATEUSDT", 1)  # $1M for the selection month
    entries = build_universe("2026-05", [quiet_then_exploded], config)

    assert entries[0].included is False
    assert entries[0].rejection is RejectionCode.LOW_LIQUIDITY


def test_average_daily_turnover_uses_selection_month_length(config):
    """February is shorter: the same turnover gives a higher daily average."""
    feb = build_universe("2026-03", [stats("AUSDT", 2800)], config)[0]
    jan = build_universe("2026-02", [stats("AUSDT", 2800)], config)[0]

    assert feb.avg_daily_turnover > jan.avg_daily_turnover


# ---------------------------------------------------------------------------
# Liquidity threshold
# ---------------------------------------------------------------------------
def test_liquid_symbol_is_included(config):
    # $50M/day × 31 days = $1550M per month. Take noticeably more.
    entries = build_universe("2026-05", [stats("SOLUSDT", 5000)], config)
    assert entries[0].included


def test_thin_symbol_is_rejected_despite_top_rank(config):
    """Rank does not save it: in a sluggish period the top can consist of thin pairs."""
    entries = build_universe("2026-05", [stats("THINUSDT", 100)], config)

    assert not entries[0].included
    assert entries[0].rejection is RejectionCode.LOW_LIQUIDITY
    assert entries[0].rank == 1


def test_top_n_limit_is_enforced(config):
    many = [stats(f"S{i}USDT", 5000 - i) for i in range(50)]
    entries = build_universe("2026-05", many, config)

    included = [e for e in entries if e.included]
    assert len(included) == config.top_n
    assert [e.rank for e in included] == list(range(1, config.top_n + 1))


def test_rank_exclusion_is_not_reported_as_low_liquidity(config):
    """Liquidity is a property of the market, rank is our knob.

    While the rank cut was called LOW_LIQUIDITY, symbols with turnover
    ten times above the threshold looked illiquid in the snapshot, and lowering
    the threshold in DEC-009 did not widen the universe by a single symbol: the freed
    slot immediately hit top_n, reporting it under someone else's code
    (DEC-015).
    """
    many = [stats(f"S{i}USDT", 5000 - i) for i in range(50)]
    entries = build_universe("2026-05", many, config)

    excluded = [e for e in entries if not e.included]
    assert excluded, "all symbols passed: nothing to check"
    assert all(e.rejection is RejectionCode.RANK_TOO_LOW for e in excluded)
    # The turnover of the cut ones is clearly above the threshold: the obstacle is not the market.
    floor = D(str(config.min_avg_daily_turnover_usd))
    assert all(e.avg_daily_turnover > floor for e in excluded)


def test_thin_symbol_beyond_the_cap_reports_liquidity_not_rank(config):
    """The reverse case: a truly illiquid symbol must keep its code
    unchanged, otherwise the new code simply replaced the old one."""
    entries = build_universe(
        "2026-05",
        [stats(f"S{i}USDT", 5000 - i) for i in range(config.top_n)]
        + [stats("THINUSDT", 0.001)],
        config,
    )
    thin = next(e for e in entries if e.symbol == "THINUSDT")
    assert thin.rejection is RejectionCode.LOW_LIQUIDITY


def test_rank_is_checked_last_of_all(config):
    """A symbol that failed on age or streams must report
    its real obstacle, even if it also does not fit into top_n."""
    crowd = [stats(f"S{i}USDT", 5000 - i) for i in range(config.top_n)]
    young = stats("YOUNGUSDT", 1, age_days=10)
    no_stream = stats("NOSTREAMUSDT", 1, streams=frozenset())

    entries = {
        e.symbol: e
        for e in build_universe(
            "2026-05", [*crowd, young, no_stream], config,
            required_streams=ALL_STREAMS,
        )
    }
    assert entries["YOUNGUSDT"].rejection is RejectionCode.TOO_YOUNG
    assert entries["NOSTREAMUSDT"].rejection is RejectionCode.MISSING_STREAM


def test_rank_reflects_turnover_order(config):
    entries = build_universe(
        "2026-05",
        [stats("SMALLUSDT", 2000), stats("BIGUSDT", 9000)],
        config,
    )
    by_symbol = {e.symbol: e for e in entries}
    assert by_symbol["BIGUSDT"].rank == 1
    assert by_symbol["SMALLUSDT"].rank == 2


# ---------------------------------------------------------------------------
# Listing age
# ---------------------------------------------------------------------------
def test_young_symbol_is_rejected_even_when_liquid(config):
    """CASHCATUSDT: 18th by turnover, listed two days ago.

    The liquidity threshold does not cut it: the turnover is real.
    """
    entries = build_universe("2026-05", [stats("CASHCATUSDT", 9000, age_days=2)], config)

    assert not entries[0].included
    assert entries[0].rejection is RejectionCode.TOO_YOUNG
    assert entries[0].listing_age_days == 2


def test_symbol_at_exactly_the_age_threshold_passes(config):
    entries = build_universe("2026-05", [stats("AUSDT", 9000, age_days=90)], config)
    assert entries[0].included


def test_symbol_one_day_short_is_rejected(config):
    entries = build_universe("2026-05", [stats("AUSDT", 9000, age_days=89)], config)
    assert entries[0].rejection is RejectionCode.TOO_YOUNG


def test_unknown_listing_date_is_rejected(config):
    """P-001: an unknown age is not treated as "probably old"."""
    unknown = SymbolMonthStats(
        symbol="XUSDT",
        turnover=D(9000) * D(1_000_000),
        bar_count=744,
        listing_date=None,
        available_streams=ALL_STREAMS,
    )
    entries = build_universe("2026-05", [unknown], config)

    assert not entries[0].included
    assert entries[0].rejection is RejectionCode.TOO_YOUNG


def test_listing_age_never_negative():
    future = datetime(2027, 1, 1, tzinfo=UTC)
    assert listing_age_days(future, datetime(2026, 1, 1, tzinfo=UTC)) == 0


# ---------------------------------------------------------------------------
# Stream completeness
# ---------------------------------------------------------------------------
def test_symbol_missing_required_stream_is_rejected(config):
    """MULTIUSDT: candles exist, OI and funding do not."""
    partial = stats(
        "MULTIUSDT", 9000, streams=frozenset({DataStream.KLINE_1H, DataStream.KLINE_15M})
    )
    entries = build_universe(
        "2026-05",
        [partial],
        config,
        required_streams=frozenset({DataStream.KLINE_1H, DataStream.OI, DataStream.FUNDING}),
    )

    assert not entries[0].included
    assert entries[0].rejection is RejectionCode.MISSING_STREAM


def test_symbol_with_all_required_streams_passes(config):
    entries = build_universe(
        "2026-05",
        [stats("SOLUSDT", 9000)],
        config,
        required_streams=frozenset({DataStream.OI, DataStream.FUNDING}),
    )
    assert entries[0].included


def test_age_is_checked_before_streams(config):
    """The reason code must point to the first real obstacle."""
    young_and_partial = stats(
        "XUSDT", 9000, age_days=5, streams=frozenset({DataStream.KLINE_1H})
    )
    entries = build_universe(
        "2026-05", [young_and_partial], config, required_streams=ALL_STREAMS
    )
    assert entries[0].rejection is RejectionCode.TOO_YOUNG


# ---------------------------------------------------------------------------
# Effective start over a set of streams
# ---------------------------------------------------------------------------
def test_effective_start_is_the_latest_of_required_streams():
    """ETHUSDT: OI since 2020-10, candles since 2021-03. Can work from March."""
    starts = {
        DataStream.OI: datetime(2020, 10, 16, tzinfo=UTC),
        DataStream.KLINE_15M: datetime(2021, 3, 10, tzinfo=UTC),
    }
    result = effective_start(starts, frozenset({DataStream.OI, DataStream.KLINE_15M}))
    assert result == datetime(2021, 3, 10, tzinfo=UTC)


def test_effective_start_ignores_streams_we_do_not_need():
    starts = {
        DataStream.OI: datetime(2020, 10, 16, tzinfo=UTC),
        DataStream.KLINE_15M: datetime(2024, 1, 1, tzinfo=UTC),
    }
    result = effective_start(starts, frozenset({DataStream.OI}))
    assert result == datetime(2020, 10, 16, tzinfo=UTC)


def test_effective_start_is_none_when_a_required_stream_is_absent():
    starts = {DataStream.KLINE_1H: datetime(2023, 1, 1, tzinfo=UTC), DataStream.OI: None}
    assert effective_start(starts, frozenset({DataStream.KLINE_1H, DataStream.OI})) is None


# ---------------------------------------------------------------------------
# Short-symbol policy
# ---------------------------------------------------------------------------
IS_START = datetime(2024, 8, 1, tzinfo=UTC)
IS_END = datetime(2026, 2, 1, tzinfo=UTC)
OOS_START = IS_END


def test_full_history_qualifies_for_fitting():
    assert qualifies_for_fitting(datetime(2023, 1, 1, tzinfo=UTC), IS_START, IS_END)


def test_partial_in_sample_does_not_qualify_for_fitting():
    """HYPEUSDT: listed 2024-11-30, in-sample started 2024-08.

    A partial sample would silently skew parameter fitting.
    """
    assert not qualifies_for_fitting(datetime(2024, 11, 30, tzinfo=UTC), IS_START, IS_END)


def test_missing_start_does_not_qualify():
    assert not qualifies_for_fitting(None, IS_START, IS_END)


def test_six_months_before_oos_qualifies():
    assert qualifies_for_oos(datetime(2025, 7, 1, tzinfo=UTC), OOS_START, 6)


def test_less_than_six_months_before_oos_does_not_qualify():
    assert not qualifies_for_oos(datetime(2025, 11, 1, tzinfo=UTC), OOS_START, 6)


def test_hype_case_end_to_end():
    """The symbol drops out of fitting but is allowed in OOS."""
    listing = datetime(2024, 11, 30, tzinfo=UTC)
    assert not qualifies_for_fitting(listing, IS_START, IS_END)
    assert qualifies_for_oos(listing, OOS_START, 6)


# ---------------------------------------------------------------------------
# Expected bar count
# ---------------------------------------------------------------------------
def test_expected_bars_scales_with_month_length():
    assert expected_bars(DataStream.KLINE_1H, "2026-01") == 24 * 31
    assert expected_bars(DataStream.KLINE_1H, "2026-02") == 24 * 28


def test_expected_bars_for_fifteen_minute_stream():
    assert expected_bars(DataStream.KLINE_15M, "2026-04") == 96 * 30


# ---------------------------------------------------------------------------
# Tradability for the selection month (DEC-015)
# ---------------------------------------------------------------------------
def _spec(symbol: str, qty_step: str):
    from src.core.models import InstrumentSpec

    return InstrumentSpec(
        symbol=symbol,
        base_coin=symbol.replace("USDT", ""),
        quote_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="",
        tick_size=D("0.01"),
        qty_step=D(qty_step),
        min_order_qty=D(qty_step),
        max_order_qty=D("1000000"),
        min_notional_value=D("5"),
        max_leverage=D("50"),
        funding_interval_minutes=480,
        launch_time=None,
        is_delisted=False,
    )


def test_tradability_follows_the_price_of_the_selection_month():
    """The very case for which the verdict became monthly.

    A symbol with a lot step of 0.01 at a price of $800 divides into nine parts,
    at $2400 into two. A single list for the whole period would mean that
    a symbol that got pricier drops out of the year before last retroactively.
    """
    from src.strategy.universe_builder import tradable_in_month

    # Expectations were computed with a $40 deposit: the input is pinned explicitly (DEC-038).
    base = load_settings()
    settings = base.model_copy(
        update={"risk": base.risk.model_copy(update={"equity_usd": 40.0})}
    )
    specs = {"ETHUSDT": _spec("ETHUSDT", "0.01")}
    prices = {("ETHUSDT", "2023-01"): D("800"), ("ETHUSDT", "2024-08"): D("2400")}

    assert "ETHUSDT" in tradable_in_month(specs, prices, ["ETHUSDT"], "2023-01", settings)
    assert "ETHUSDT" not in tradable_in_month(specs, prices, ["ETHUSDT"], "2024-08", settings)


def test_verdict_reads_only_the_month_it_was_asked_about():
    """Otherwise "point-in-time" stays a word: a neighbouring month in the same
    data must not affect the answer."""
    from src.strategy.universe_builder import tradable_in_month

    settings = load_settings()
    specs = {"XUSDT": _spec("XUSDT", "0.01")}
    cheap = {("XUSDT", "2024-01"): D("10")}
    both = {**cheap, ("XUSDT", "2024-02"): D("99999")}

    assert tradable_in_month(specs, cheap, ["XUSDT"], "2024-01", settings) == (
        tradable_in_month(specs, both, ["XUSDT"], "2024-01", settings)
    )


def test_symbol_without_price_or_spec_is_not_declared_tradable():
    """P-001: "nothing to check with" is not "checked that it is allowed".
    Such symbols are handled by the unverifiable mechanism, not this one."""
    from src.strategy.universe_builder import tradable_in_month

    settings = load_settings()
    specs = {"AUSDT": _spec("AUSDT", "0.01")}

    assert tradable_in_month(specs, {}, ["AUSDT"], "2024-01", settings) == frozenset()
    assert tradable_in_month(
        {}, {("BUSDT", "2024-01"): D("10")}, ["BUSDT"], "2024-01", settings
    ) == frozenset()
    assert tradable_in_month(
        specs, {("AUSDT", "2024-01"): D("0")}, ["AUSDT"], "2024-01", settings
    ) == frozenset()


def test_fine_grained_symbol_stays_tradable_at_any_price():
    """Control: the rule must cut by GRANULARITY, not by price
    by itself."""
    from src.strategy.universe_builder import tradable_in_month

    settings = load_settings()
    specs = {"CUSDT": _spec("CUSDT", "0.0001")}
    prices = {("CUSDT", "2024-01"): D("2400")}

    assert "CUSDT" in tradable_in_month(specs, prices, ["CUSDT"], "2024-01", settings)
