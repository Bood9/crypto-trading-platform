"""Building the point-in-time universe (DEC-003).

Key property: the universe for month M is built from the turnover of month M−1.
Selecting by turnover of the period we trade in would be lookahead — we would be
picking coins knowing they turn out to be liquid.

Turnover comes from the candles' `turnover` field, so we already have the
historical data for selection and need no external source.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from src.core.config import UniverseConfig
from src.core.logging import get_logger
from src.core.models import DataStream, RejectionCode

log = get_logger(__name__)


def month_key(moment: datetime | date) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


def previous_month(key: str, lag: int = 1) -> str:
    """Month key `lag` months back."""
    if lag < 1:
        raise ValueError(f"lag must be at least 1, got {lag}")
    year, month = (int(x) for x in key.split("-"))
    total = year * 12 + (month - 1) - lag
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_bounds(key: str) -> tuple[datetime, datetime]:
    """Month bounds: start inclusive, end exclusive."""
    year, month = (int(x) for x in key.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    end = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=UTC)
    )
    return start, end


def days_in_month(key: str) -> int:
    start, end = month_bounds(key)
    return (end - start).days


@dataclass(frozen=True)
class SymbolMonthStats:
    """Symbol data for the selection month (i.e. for M−1)."""

    symbol: str
    turnover: Decimal
    bar_count: int
    listing_date: datetime | None
    available_streams: frozenset[DataStream] = frozenset()


@dataclass(frozen=True)
class UniverseEntry:
    month: str
    symbol: str
    turnover_prev_period: Decimal
    avg_daily_turnover: Decimal
    rank: int
    included: bool
    rejection: RejectionCode | None = None
    listing_age_days: int | None = None


def listing_age_days(listing: datetime | None, at: datetime) -> int | None:
    if listing is None:
        return None
    return max(0, (at - listing).days)


def _first_obstacle(
    item: SymbolMonthStats,
    *,
    age: int | None,
    avg_daily: Decimal,
    config: UniverseConfig,
    required_streams: frozenset[DataStream],
    slots_left: int,
    tradable: frozenset[str] | None = None,
    unverifiable: frozenset[str] = frozenset(),
    include_unverifiable: bool = True,
) -> RejectionCode | None:
    """The first real obstacle, or None if the symbol passes.

    The order of checks is part of the contract, not an implementation detail:
    the reason code in the snapshot must point at the real obstacle. If the rank
    were checked before the data, a symbol without OI would get LOW_LIQUIDITY,
    and when analysing the backtest we would look for a liquidity problem where
    there is none.
    """
    # Tradability. Checked first: a symbol we physically cannot trade with
    # our deposit must not get into the backtest at all. BTCUSDT passes on
    # turnover and age, but its lot step equals the whole position
    # (NO_PARTIAL_EXIT).
    #
    # Three states, not two. "Checked and not possible" and "nothing to
    # check with" are different things and must not be mixed.
    #
    # Delisted pairs are absent from instruments-info: no lot step, no price.
    # Rejecting them as unusable would bring back the survivorship bias that
    # DEC-006 specifically removes: 18 dead pairs were in the top 30 by
    # turnover.
    #
    # So they are included, flagged, and the share of trades on them goes
    # into the backtest report — exactly as with short-history symbols.
    if tradable is not None and item.symbol not in tradable:
        if item.symbol in unverifiable:
            if not include_unverifiable:
                return RejectionCode.UNVERIFIABLE_TRADABILITY
        else:
            return RejectionCode.NO_PARTIAL_EXIT

    # Age. This rule is stricter than the short-symbol policy and applies
    # before it: a freshly listed coin produces anomalous funding and OI spikes
    # because of an immature market — exactly the features Setup A looks at.
    # The model would love them, and there is nothing in the history to
    # check that against.
    # P-001: an unknown listing date is no reason to let it through.
    if config.min_listing_age_days > 0 and (age is None or age < config.min_listing_age_days):
        return RejectionCode.TOO_YOUNG

    # Stream completeness for the selection month. Counted per
    # (symbol, month, stream) triple: MULTIUSDT provides candles but not
    # OI or funding, and this is a class of cases, not an exception.
    if required_streams and not required_streams <= item.available_streams:
        return RejectionCode.MISSING_STREAM

    # Absolute liquidity threshold. Rank in a sluggish period may let through
    # a pair on which the stop would slip.
    if avg_daily < Decimal(str(config.min_avg_daily_turnover_usd)):
        return RejectionCode.LOW_LIQUIDITY

    # Rank. Checked LAST and with its own code: the symbol got this far, so
    # it is fit on age, streams and turnover, and the obstacle is our knob,
    # not the market. While this rejection was called LOW_LIQUIDITY,
    # 906 symbol-months with turnover above the threshold looked illiquid,
    # and lowering the threshold in DEC-009 did not widen the universe by a
    # single symbol: the freed slot immediately ran into top_n (DEC-015).
    if slots_left <= 0:
        return RejectionCode.RANK_TOO_LOW

    return None


def build_universe(
    month: str,
    stats: list[SymbolMonthStats],
    config: UniverseConfig,
    *,
    required_streams: frozenset[DataStream] = frozenset(),
    tradable: frozenset[str] | None = None,
    unverifiable: frozenset[str] = frozenset(),
    include_unverifiable: bool = True,
) -> list[UniverseEntry]:
    """Universe composition for month `month`.

    `stats` must refer to the selection month (M − lag), not to `month`.
    The calling code is responsible for this; the lookahead test checks the
    whole chain.

    The order of checks matters for debugging: hard facts about the symbol
    first (age, stream completeness), then liquidity, and only then rank.
    That way the reason code in the snapshot points at the first real obstacle,
    not at a rank the symbol failed to reach because data was missing.
    """
    selection_month = previous_month(month, config.selection_lag_months)
    _, selection_end = month_bounds(selection_month)
    days = days_in_month(selection_month)

    ranked = sorted(stats, key=lambda s: s.turnover, reverse=True)
    entries: list[UniverseEntry] = []
    accepted = 0

    for position, item in enumerate(ranked, start=1):
        avg_daily = item.turnover / Decimal(days) if days else Decimal(0)
        age = listing_age_days(item.listing_date, selection_end)

        rejection = _first_obstacle(
            item,
            age=age,
            avg_daily=avg_daily,
            config=config,
            required_streams=required_streams,
            slots_left=config.top_n - accepted,
            tradable=tradable,
            unverifiable=unverifiable,
            include_unverifiable=include_unverifiable,
        )

        if rejection is None:
            accepted += 1

        entries.append(
            UniverseEntry(
                month=month,
                symbol=item.symbol,
                turnover_prev_period=item.turnover,
                avg_daily_turnover=avg_daily,
                rank=position,
                included=rejection is None,
                rejection=rejection,
                listing_age_days=age,
            )
        )

    log.info(
        "universe.built",
        month=month,
        selection_month=selection_month,
        candidates=len(ranked),
        included=accepted,
    )
    return entries


def months_between(start: datetime, end: datetime) -> list[str]:
    """List of months from `start` to `end` inclusive."""
    keys: list[str] = []
    cursor = datetime(start.year, start.month, 1, tzinfo=UTC)
    while cursor <= end:
        keys.append(month_key(cursor))
        cursor = (
            datetime(cursor.year + 1, 1, 1, tzinfo=UTC)
            if cursor.month == 12
            else datetime(cursor.year, cursor.month + 1, 1, tzinfo=UTC)
        )
    return keys


def expected_bars(stream: DataStream, month: str) -> int:
    """How many bars of the stream a month should have at full coverage.

    The base for the fill ratio in the availability matrix: without it "there
    is data" and "there are three bars out of seven hundred" are indistinguishable.
    """
    per_day = {
        DataStream.KLINE_15M: 96,
        DataStream.KLINE_1H: 24,
        DataStream.KLINE_4H: 6,
        DataStream.OI: 24,
        DataStream.FUNDING: 3,  # at an 8-hour interval
    }
    if stream not in per_day:
        raise ValueError(f"unknown stream {stream!r}")
    return per_day[stream] * days_in_month(month)


def effective_start(starts: dict[DataStream, datetime | None], required: frozenset[DataStream]):
    """The moment from which ALL required streams are available.

    The maximum, not the minimum and not the listing date: for some pairs OI
    starts earlier than candles (ETHUSDT — OI from 2020-10, candles from 2021-03),
    so a single date per symbol would be wrong for at least one of the streams.

    None means at least one required stream is missing entirely.
    """
    moments: list[datetime] = []
    for stream in required:
        value = starts.get(stream)
        if value is None:
            return None
        moments.append(value)
    return max(moments) if moments else None


def qualifies_for_fitting(
    symbol_start: datetime | None,
    in_sample_start: datetime,
    in_sample_end: datetime,
) -> bool:
    """Whether parameters may be tuned on this symbol.

    Full coverage of the in-sample window is required: a symbol that appeared
    in the middle would give the parameters a partial sample and quietly bias
    the fit.
    """
    if symbol_start is None:
        return False
    return symbol_start <= in_sample_start < in_sample_end


def qualifies_for_oos(
    symbol_start: datetime | None,
    oos_start: datetime,
    min_months_before: int,
) -> bool:
    """Admission to out-of-sample with a shortened history."""
    if symbol_start is None:
        return False
    required = timedelta(days=min_months_before * 30)
    return oos_start - symbol_start >= required
