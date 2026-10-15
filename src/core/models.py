"""Contracts between modules. Pydantic v2, spec §8.

Only what is already in use. Models for signals, risk decisions and orders
will come with the corresponding phases — creating them in advance would
mean fixing the form before the content becomes clear.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RejectionCode(StrEnum):
    """Rejection codes. Spec §5: the reason is always logged.

    The values are the main source of debugging, so the set is closed: free
    text in this field would make rejection statistics impossible to
    aggregate.
    """

    MIN_NOTIONAL = "MIN_NOTIONAL"
    LIQ_BUFFER = "LIQ_BUFFER"
    MAX_POSITIONS = "MAX_POSITIONS"
    MAX_MARGIN = "MAX_MARGIN"
    CORRELATION = "CORRELATION"
    DAILY_STOP = "DAILY_STOP"
    WEEKLY_STOP = "WEEKLY_STOP"
    AI_VETO = "AI_VETO"
    REGIME_FILTER = "REGIME_FILTER"
    MODE_OFF = "MODE_OFF"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    EXCHANGE_ERROR = "EXCHANGE_ERROR"

    # Phase 1: universe selection
    NOT_CRYPTO = "NOT_CRYPTO"
    NOT_TRADING = "NOT_TRADING"
    QTY_GRANULARITY = "QTY_GRANULARITY"
    NO_PARTIAL_EXIT = "NO_PARTIAL_EXIT"
    # Nothing to check tradability with: the symbol is not in instruments-info,
    # i.e. neither the lot step nor the current price is known. Differs from
    # NO_PARTIAL_EXIT, where unsuitability is ESTABLISHED.
    UNVERIFIABLE_TRADABILITY = "UNVERIFIABLE_TRADABILITY"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    # The symbol passed ALL substantive checks and did not get into the universe
    # only because the list is cut at top_n. A separate code, not
    # LOW_LIQUIDITY: liquidity is a market property, rank is our knob,
    # and confusing them means looking for the problem in the wrong place.
    # Exactly this substitution hid that the universe width is set by an arbitrary number
    # (DEC-015).
    RANK_TOO_LOW = "RANK_TOO_LOW"
    WIDE_SPREAD = "WIDE_SPREAD"
    MISSING_STREAM = "MISSING_STREAM"
    SHORT_HISTORY = "SHORT_HISTORY"
    TOO_YOUNG = "TOO_YOUNG"
    UNKNOWN_INSTRUMENT_TYPE = "UNKNOWN_INSTRUMENT_TYPE"
    MISSING_MMR = "MISSING_MMR"


class DataStream(StrEnum):
    """Data streams.

    Availability is computed per triple (symbol, month, stream), not per symbol:
    MULTIUSDT serves candles but not OI and funding — this is a class of cases,
    not an exception.
    """

    KLINE_15M = "kline_15m"
    KLINE_1H = "kline_1h"
    KLINE_4H = "kline_4h"
    OI = "oi"
    FUNDING = "funding"


class InstrumentSpec(BaseModel):
    """Instrument parameters. Only from the API, no hardcoding (spec §16).

    Decimal, not float: qty_step and tick_size are used to round the order
    size, and an accumulated binary fraction error here turns into an
    exchange rejection.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    base_coin: str
    quote_coin: str
    contract_type: str
    status: str

    # The authoritative asset class flag. Empty string — crypto,
    # "innovation" — crypto from the innovation zone, "stock" —
    # tokenized stocks, "commodity" — commodities and metals.
    # It cannot be told by the base coin: a manual list lags behind
    # listings, and there are hundreds of them.
    symbol_type: str = ""

    tick_size: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    max_order_qty: Decimal
    min_notional_value: Decimal

    max_leverage: Decimal
    funding_interval_minutes: int
    launch_time: datetime | None = None

    # From /v5/market/risk-limit, the first tier. At our sizes we are always
    # in it, but the value is still pulled from the API.
    mmr: Decimal | None = None
    initial_margin_rate: Decimal | None = None
    risk_limit_value: Decimal | None = None

    # Whether the symbol is alive. Dead ones are restored from the archive and
    # are missing from instruments-info.
    is_delisted: bool = False

    @property
    def is_trading(self) -> bool:
        return self.status == "Trading"


class Tradability(BaseModel):
    """Result of the check "can we trade this at all with $40"."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    price: Decimal
    turnover_24h_usd: Decimal

    # max(minNotionalValue, minOrderQty × price).
    # The constraint usually comes from minOrderQty, not minNotionalValue:
    # for BTCUSDT minNotionalValue = 5 USDT, but 0.001 BTC × price ≈ $65.
    effective_min_notional: Decimal

    # qtyStep × price — the step by which we can change the position size.
    granularity_usd: Decimal

    # Into how many parts the target notional divides. 1 means the position
    # is whole only and partial exits are impossible.
    max_parts: int

    target_notional: Decimal
    tradable: bool
    rejection: RejectionCode | None = None
    note: str | None = None

    mmr: Decimal | None = None

    @property
    def supports_partial_exit(self) -> bool:
        return self.max_parts >= 2


class MonthlyAvailability(BaseModel):
    """Availability of one stream for a symbol over one month.

    A row of the matrix (symbol, month, stream). A symbol's admission to a
    setup for a month is the intersection of the streams the setup requires.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    stream: DataStream
    available: bool
    bar_count: int = 0
    expected_count: int = 0

    @property
    def completeness(self) -> float:
        if self.expected_count <= 0:
            return 0.0
        return min(1.0, self.bar_count / self.expected_count)


class StreamStart(BaseModel):
    """Start date of one stream for a symbol.

    Stored separately per stream: for some pairs OI starts earlier than
    15m candles (ETHUSDT — OI from 2020-10, candles from 2021-03), so a single
    "listing date" per symbol would be wrong for both streams.
    """

    model_config = ConfigDict(frozen=True)

    symbol: str
    stream: DataStream
    first_ts: datetime | None
    last_ts: datetime | None = None

    @property
    def available(self) -> bool:
        return self.first_ts is not None
