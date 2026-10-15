"""Setup A — Open Interest compression. Spec §6, DEC-001.

The trigger is defined as a **sharp hourly Open Interest compression in the
direction of the price move**. This is a DEFINITION, not an approximation of
liquidations: Bybit has no liquidation history, and the same definition works
live. Replacing it with real liquidations would require a full re-backtest
from scratch (DEC-001).

Logic: OI compression means forced position closing. The cascade pushes the
price further than justified, and exhaustion is often followed by a pullback.
We enter against the cascade's move.

Exit at the cascade VWAP (DEC-014). The R-multiple target was set in the spec
before it was known what move the market gives at all on this horizon; VWAP,
by contrast, is not an assigned number but the average price at which the
cascade closed positions, i.e. the level the price returns to if the pullback
hypothesis holds.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from src.backtest.engine import MarketBar, Signal
from src.backtest.funnel import Funnel
from src.backtest.indicators import Bar, atr, true_range
from src.core.logging import get_logger
from src.strategy.setup_b import SymbolContext, percentile_rank

log = get_logger(__name__)


#: The cascade window. ONE value for three uses: OI compression is measured
#: over it, the extreme for the stop is taken over it, the VWAP is computed over it.
#:
#: Three separate parameters here would be not three settings but three
#: different definitions of the cascade inside one setup. A stop "beyond the
#: cascade extreme" over one bar and a VWAP "from the start of the cascade" over
#: four describe different events, and the mismatch never shows itself —
#: the numbers get computed, the report gets printed.
CASCADE_WINDOW = timedelta(hours=1)


class ExitMode(StrEnum):
    """What sets the position target.

    A closed enumeration (P-001): an unknown value in the config is an
    error at startup, not a silent fallback to the default.
    """

    #: Target at the cascade VWAP — the average forced-closing price.
    VWAP = "vwap"
    #: Target in R multiples. Kept for comparison, not for trading.
    FIXED_R = "fixed_r"


def parse_timeframe(text: str) -> timedelta:
    """`15m`, `1h`, `4h` → bar duration.

    Needed because Bybit candles are dated by the START of the period, while
    the decision is made at its close. Without the bar duration one cannot be
    converted into the other, and the difference leaks into lookahead (see AtrSeries).
    """
    units = {"m": "minutes", "h": "hours", "d": "days"}
    value, suffix = text[:-1], text[-1:]
    if suffix not in units or not value.isdigit():
        raise ValueError(f"cannot parse timeframe {text!r}")
    return timedelta(**{units[suffix]: int(value)})


@dataclass
class AtrSeries:
    """ATR computed on ITS OWN timeframe and available by time.

    Spec §6 sets the stop as 1.5 × ATR(14, 1h), and entry on a 15-minute
    candle. Computing ATR on 15m bars would mean taking a different value:
    on a four-times-finer grid ATR is roughly half as large, and the stop
    would end up twice as close as declared.

    **The series is keyed by the CLOSE time of the hourly bar, not the open.**
    Bybit candles are dated by the start of the period (`bar[0]` — `startTime`),
    so "the last value no later than the moment" by open time would return the
    ATR of an hour that is still in progress at decision time: for an entry on
    the 15-minute bar 10:00–10:15 that is 45 minutes of the future inside the
    stop distance and inside the candle-size filter. The error neither crashes
    nor gets logged — it just makes stops more precise than is possible.
    """

    #: The time by which the value is fully known.
    closes: list[datetime]
    values: list[Decimal]

    def at(self, moment: datetime) -> Decimal | None:
        """The last value CLOSED no later than the moment.

        `moment` is the decision moment, i.e. the close of the entry bar,
        not its open.
        """
        idx = bisect_right(self.closes, moment) - 1
        return self.values[idx] if idx >= 0 else None


def build_atr_series(
    bars: list[MarketBar], period: int, interval: timedelta
) -> AtrSeries:
    """Wilder's ATR over the whole series in a single pass.

    Calling `atr()` on every prefix is not an option: it is quadratic, and on
    seventeen thousand hourly bars turns into hundreds of millions of
    operations for seventeen thousand numbers.

    The value at index `j` equals `atr(bars[: j + 1], period)` on clean
    data — this is pinned by a test, because a second implementation of the
    same formula must agree with the first, otherwise there is no point
    in it.

    There is one deliberate difference: a non-numeric bar (the LUNA-level
    collapse) here resets the accumulation and restarts the warmup, whereas
    `atr()` returns None for the whole window. The recursive sum must not be
    poisoned by such a bar — it would carry on through the rest of the series.
    """
    if period < 1:
        raise ValueError(f"period must be positive, got {period}")

    closes: list[datetime] = []
    values: list[Decimal] = []

    warmup: list[Decimal] = []
    value: Decimal | None = None
    previous_close: Decimal | None = None

    for bar in bars:
        if not (
            bar.high.is_finite() and bar.low.is_finite() and bar.close.is_finite()
        ):
            warmup.clear()
            value = None
            previous_close = None
            continue

        tr = true_range(Bar(bar.high, bar.low, bar.close), previous_close)
        previous_close = bar.close

        try:
            if value is None:
                warmup.append(tr)
                if len(warmup) < period:
                    continue
                value = sum(warmup) / Decimal(period)
            else:
                value = (value * (period - 1) + tr) / Decimal(period)
        except (InvalidOperation, ZeroDivisionError, OverflowError):
            warmup.clear()
            value = None
            previous_close = None
            continue

        if not value.is_finite() or value < 0:
            warmup.clear()
            value = None
            previous_close = None
            continue

        closes.append(bar.ts + interval)
        values.append(value)

    return AtrSeries(closes=closes, values=values)


@dataclass(frozen=True)
class SetupAParams:
    """Parameters from config/settings.yaml, section strategy.setup_a.

    All fixed BEFORE the first run (DEC-001, condition 2).
    """

    oi_drop_pct_1h: Decimal
    candle_range_atr_mult: Decimal
    require_direction_match: bool
    volume_zscore_min: float
    volume_zscore_lookback_days: int
    funding_upper_percentile: float
    funding_lower_percentile: float
    funding_percentile_lookback_days: int
    entry_timeframe: str
    stop_atr_mult: Decimal
    stop_atr_period: int
    exit_mode: ExitMode
    target_r: Decimal
    time_stop_hours: int

    #: Filter sweeps. An empty list means "no sweep given" and is rejected
    #: at load: a knob without a sweep is a knob whose sensitivity nobody
    #: has checked (DEC-007, DEC-015).
    oi_drop_pct_1h_sensitivity: tuple[Decimal, ...] = ()
    candle_range_atr_mult_sensitivity: tuple[Decimal, ...] = ()
    volume_zscore_min_sensitivity: tuple[float, ...] = ()
    funding_percentile_sensitivity: tuple[float, ...] = ()

    @property
    def entry_interval(self) -> timedelta:
        return parse_timeframe(self.entry_timeframe)

    @property
    def volume_zscore_lookback_bars(self) -> int:
        """Volume z-score window in bars of the ENTRY timeframe.

        Counting it in hours while entries are searched on 15-minute bars
        means taking a window four times shorter than declared in the config:
        `7 days × 24` on a 15m grid is 42 hours, not seven days.
        """
        seconds = self.entry_interval.total_seconds()
        return int(self.volume_zscore_lookback_days * 86_400 / seconds)

    @classmethod
    def from_config(cls, cfg: dict) -> SetupAParams:
        mode = str(cfg.get("exit_mode", ExitMode.VWAP.value))
        try:
            exit_mode = ExitMode(mode)
        except ValueError as exc:  # P-001: unknown value — refuse
            allowed = ", ".join(m.value for m in ExitMode)
            raise ValueError(
                f"strategy.setup_a.exit_mode = {mode!r}, allowed: {allowed}"
            ) from exc
        sweeps = {
            "oi_drop_pct_1h": [
                Decimal(str(v)) for v in cfg.get("oi_drop_pct_1h_sensitivity", [])
            ],
            "candle_range_atr_mult": [
                Decimal(str(v)) for v in cfg.get("candle_range_atr_mult_sensitivity", [])
            ],
            "volume_zscore_min": [
                float(v) for v in cfg.get("volume_zscore_min_sensitivity", [])
            ],
            "funding_lower_percentile": [
                float(v) for v in cfg.get("funding_percentile_sensitivity", [])
            ],
        }
        for knob, grid in sweeps.items():
            if not grid:
                raise ValueError(
                    f"knob strategy.setup_a.{knob} has no sweep: "
                    "nobody has checked how sensitive the result is to it"
                )
            base = cfg[knob]
            if type(grid[0])(str(base)) not in grid:
                raise ValueError(
                    f"strategy.setup_a.{knob}={base} is missing from its own "
                    f"sweep {grid} — there would be nothing to compare it with"
                )

        return cls(
            oi_drop_pct_1h=Decimal(str(cfg["oi_drop_pct_1h"])),
            candle_range_atr_mult=Decimal(str(cfg["candle_range_atr_mult"])),
            require_direction_match=bool(cfg["require_direction_match"]),
            volume_zscore_min=float(cfg["volume_zscore_min"]),
            volume_zscore_lookback_days=int(cfg["volume_zscore_lookback_days"]),
            funding_upper_percentile=float(cfg["funding_upper_percentile"]),
            funding_lower_percentile=float(cfg["funding_lower_percentile"]),
            funding_percentile_lookback_days=int(cfg["funding_percentile_lookback_days"]),
            entry_timeframe=str(cfg.get("entry_timeframe", "15m")),
            stop_atr_mult=Decimal(str(cfg["stop_atr_mult"])),
            stop_atr_period=int(cfg["stop_atr_period"]),
            exit_mode=exit_mode,
            target_r=Decimal(str(cfg["target_r"])),
            time_stop_hours=int(cfg["time_stop_hours"]),
            oi_drop_pct_1h_sensitivity=tuple(sweeps["oi_drop_pct_1h"]),
            candle_range_atr_mult_sensitivity=tuple(sweeps["candle_range_atr_mult"]),
            volume_zscore_min_sensitivity=tuple(sweeps["volume_zscore_min"]),
            funding_percentile_sensitivity=tuple(sweeps["funding_lower_percentile"]),
        )


def oi_compression(
    context: SymbolContext, moment, window: timedelta
) -> Decimal | None:
    """OI compression over the window, in percent. Positive — a decline.

    None means there is nothing to judge by: data is missing at one end of the
    window. A missing answer is not equated with zero (P-001) — otherwise a
    symbol without OI would look like a symbol with stable OI.
    """
    now = context.oi_at(moment)
    past = context.oi_at(moment - window)
    if now is None or past is None or past <= 0:
        return None
    return (past - now) / past * 100


def cascade_bars(
    window, moment: datetime, span: timedelta = CASCADE_WINDOW
) -> list[MarketBar]:
    """Cascade bars: from the first bar of the compression window to the entry bar inclusive.

    Selected by time, not by bar count: gaps in the series exist, and "the
    last four bars" on a gappy stretch would mean a window longer than an
    hour. Here the window is exactly the one the OI compression was
    computed over.
    """
    out: list[MarketBar] = []
    for i in range(len(window) - 1, -1, -1):
        bar = window[i]
        if moment - bar.ts >= span:
            break
        out.append(bar)
    out.reverse()
    return out


def cascade_vwap(bars: list[MarketBar]) -> Decimal | None:
    """Cascade VWAP: typical price (H+L+C)/3 weighted by volume.

    This is the average price at which the cascade closed positions. The
    setup's hypothesis is precisely that the price returns to it.

    None for zero or non-computable volume (P-001). Falling back to a simple
    average is forbidden here: it would pass "no volume" off as "flat volume",
    while it is exactly the volume distribution inside the cascade that
    carries the whole meaning of the value.
    """
    weighted = Decimal(0)
    total = Decimal(0)
    for bar in bars:
        volume = bar.volume
        if volume is None or not volume.is_finite() or volume < 0:
            return None
        if not (bar.high.is_finite() and bar.low.is_finite() and bar.close.is_finite()):
            return None
        typical = (bar.high + bar.low + bar.close) / 3
        weighted += typical * volume
        total += volume
    if total <= 0:
        return None
    try:
        result = weighted / total
    except (InvalidOperation, ZeroDivisionError, OverflowError):
        return None
    return result if result.is_finite() and result > 0 else None


def volume_zscore(bars, lookback: int) -> float | None:
    """How far the last bar's volume is above normal, in sigmas.

    The window is taken WHOLE or not at all. Trimming it to the available
    history would mean computing a different statistic under the same name:
    a z-score over forty bars and over seven hundred are different values,
    and during warmup the former is systematically inflated.
    """
    if lookback < 2 or len(bars) < lookback + 1:
        return None
    history = [float(b.volume) for b in bars[-(lookback + 1) : -1]]
    mean = sum(history) / len(history)
    variance = sum((v - mean) ** 2 for v in history) / len(history)
    stdev = variance**0.5
    if stdev <= 0:
        return None
    return (float(bars[-1].volume) - mean) / stdev


def warmup_bars(params: SetupAParams) -> int:
    """How many bars are needed for all checks to work at full strength."""
    return max(params.volume_zscore_lookback_bars + 1, params.stop_atr_period + 1, 21)


def make_signal_fn(
    context: SymbolContext,
    params: SetupAParams,
    atr_series: AtrSeries | None = None,
    funnel: Funnel | None = None,
) -> callable:
    """Signal function for a single symbol.

    Entry against the cascade's move: OI compression on a falling price gives
    a long, on a rising one — a short.

    `funnel` is observation only: a counter of entries into each stage. No
    check depends on it, and with `None` the same code runs. Without it,
    frequency calibration comes down to tuning thresholds blindly: you see
    how many trades came out, but not where the rest dropped out.
    """
    vol_lookback = params.volume_zscore_lookback_bars
    entry_interval = params.entry_interval
    need = warmup_bars(params)

    def mark(stage: str) -> None:
        if funnel is not None:
            funnel.enter(stage)

    def signal_fn(window) -> Signal | None:
        mark("bars")
        if len(window) < need:
            return None
        mark("warmup")

        current = window[-1]
        # The decision is made at the bar's CLOSE, while the bar is dated by the
        # start of the period. Everything checked by time is checked against the close.
        decided_at = current.ts + entry_interval

        # 1. Trigger: a sharp OI compression over the cascade window.
        drop = oi_compression(context, current.ts, CASCADE_WINDOW)
        if drop is None or drop < params.oi_drop_pct_1h:
            return None
        mark("oi_drop")

        # 2. Candle size in ATR — confirmation that the compression came
        # with a move and not merely with positions closing.
        #
        # ATR is taken from the HOURLY timeframe (spec §6), even when entries
        # are searched on 15-minute bars: these are different values.
        if atr_series is not None:
            atr_value = atr_series.at(decided_at)
        else:
            atr_value = atr(
                [Bar(b.high, b.low, b.close) for b in window], params.stop_atr_period
            )
        if atr_value is None or atr_value <= 0:
            return None
        mark("atr_known")
        candle_range = current.high - current.low
        if candle_range < atr_value * params.candle_range_atr_mult:
            return None
        mark("candle_range")

        # 3. Direction: we enter AGAINST the cascade's move.
        moved_down = current.close < current.open
        is_long = moved_down

        # 4. Volume: a cascade is a spike, not a quiet slide.
        z = volume_zscore(window, vol_lookback)
        if z is None or z < params.volume_zscore_min:
            return None
        mark("volume_z")

        # 5. Funding at an extreme: the crowd has piled into one side.
        rates = context.funding_window(
            current.ts, params.funding_percentile_lookback_days
        )
        latest = context.latest_funding(current.ts)
        if latest is None:
            return None
        rank = percentile_rank(rates, latest)
        if rank is None:
            return None
        # Overheated long (high funding) → short, and vice versa.
        if is_long and rank > params.funding_lower_percentile:
            return None
        if not is_long and rank < params.funding_upper_percentile:
            return None
        mark("funding")

        # 6. Stop beyond the cascade extreme, by ATR. The extreme is taken over
        # the WHOLE cascade, not over the single entry bar: otherwise the stop
        # sits beyond the extreme of the event's last candle, which is not the
        # same as beyond the extreme of the event.
        cascade = cascade_bars(window, current.ts, CASCADE_WINDOW)
        if not cascade:
            return None
        mark("cascade")

        entry = current.close
        if is_long:
            stop = min(b.low for b in cascade) - atr_value * params.stop_atr_mult
        else:
            stop = max(b.high for b in cascade) + atr_value * params.stop_atr_mult

        risk = abs(entry - stop)
        if risk <= 0 or stop <= 0:
            return None
        mark("risk_positive")
        if funnel is not None and entry > 0:
            funnel.record_stop(risk / entry * 100)

        # 7. Target.
        if params.exit_mode is ExitMode.VWAP:
            level = cascade_vwap(cascade)
            if level is None:
                return None
            # The target must be ahead in the trade's direction. A VWAP behind the
            # entry means the price has already returned into the cascade range —
            # there is nowhere to return to, and the trade would degenerate into an
            # instant exit on the very first bar.
            advance = level - entry if is_long else entry - level
            if advance <= 0:
                return None
            target = level
        else:
            target = (
                entry + risk * params.target_r
                if is_long
                else entry - risk * params.target_r
            )

        mark("target")
        mark("signal")
        return Signal(is_long=is_long, entry=entry, stop=stop, target=target)

    return signal_fn


__all__ = [
    "CASCADE_WINDOW",
    "AtrSeries",
    "ExitMode",
    "SetupAParams",
    "build_atr_series",
    "cascade_bars",
    "cascade_vwap",
    "make_signal_fn",
    "oi_compression",
    "parse_timeframe",
    "volume_zscore",
    "warmup_bars",
]
