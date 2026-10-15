"""Position sizing. Spec §4.

Pure arithmetic with no network or DB access: everything it needs comes in
as arguments. This is not a matter of style — the module must be fully
covered by tests, and a computation that reaches out for data costs three
times as much to test.

Key rule: **leverage is not set, it is computed** from the distance
to the stop. `leverage_cap` is a ceiling, not a target.

Decimal, not float, throughout the chain. Size is rounded to the lot step,
and accumulated binary-fraction error ends up as an exchange rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext

from src.core.models import RejectionCode

#: Precision headroom for intermediate computations. The result is rounded
#: to the lot step anyway, but divisions along the way must not lose
#: significant digits.
_PRECISION = 40


@dataclass(frozen=True)
class SizingInput:
    equity: Decimal
    entry_price: Decimal
    stop_price: Decimal
    #: From /v5/market/risk-limit, first tier. Hardcoding is forbidden (spec §16).
    mmr: Decimal
    qty_step: Decimal
    min_order_qty: Decimal
    min_notional: Decimal

    margin_pct: Decimal
    max_loss_pct: Decimal
    leverage_cap: Decimal
    min_liq_buffer_ratio: Decimal
    #: Position size cap as a percentage of the deposit (DEC-040). None means
    #: no cap, which is how all past runs were computed.
    max_position_pct: Decimal | None = None

    @property
    def is_long(self) -> bool:
        return self.stop_price < self.entry_price


@dataclass(frozen=True)
class SizingResult:
    approved: bool
    rejection: RejectionCode | None = None
    note: str | None = None

    qty: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    margin: Decimal = Decimal(0)
    leverage: Decimal = Decimal(0)
    stop_distance_pct: Decimal = Decimal(0)
    loss_at_stop: Decimal = Decimal(0)
    liq_price: Decimal = Decimal(0)
    liq_distance_pct: Decimal = Decimal(0)
    liq_buffer_ratio: Decimal = Decimal(0)


def stop_distance(entry: Decimal, stop: Decimal) -> Decimal:
    """Distance to the stop as a fraction of the entry price.

    Always positive: the trade direction does not matter here, only the
    size of the move to the stop does.
    """
    if entry <= 0:
        raise ValueError(f"entry price must be positive, got {entry}")
    if stop <= 0:
        raise ValueError(f"stop price must be positive, got {stop}")
    return abs(entry - stop) / entry


def max_leverage_for_stop(
    stop_dist: Decimal, mmr: Decimal, buffer_ratio: Decimal
) -> Decimal | None:
    """Maximum leverage at which the stop stays closer than liquidation.

    The condition `1/L − MMR >= buffer × stop_dist` implies
    `L <= 1 / (buffer × stop_dist + MMR)`.

    None means no suitable leverage exists: the denominator is
    non-positive, or the stop is so far away that even leverage 1
    does not give the required buffer.
    """
    denominator = buffer_ratio * stop_dist + mmr
    if denominator <= 0:
        return None
    leverage = Decimal(1) / denominator
    return leverage if leverage >= 1 else None


def liquidation_price(
    entry: Decimal, leverage: Decimal, mmr: Decimal, *, is_long: bool
) -> Decimal:
    """Liquidation price, a simplified estimate for isolated margin.

    Distance to liquidation ≈ `1/L − MMR` (spec §3). The exchange's exact
    formula accounts for the MM deduction and the closing fee; for the
    buffer check this approximation is enough, and it is more
    conservative — it understates the distance.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    distance = Decimal(1) / leverage - mmr
    if is_long:
        return entry * (Decimal(1) - distance)
    return entry * (Decimal(1) + distance)


def round_qty_down(qty: Decimal, step: Decimal) -> Decimal:
    """Round down to the lot step.

    Down only: rounding up would grow the position beyond the computed
    size, i.e. silently break the loss limit.
    """
    if step <= 0:
        raise ValueError("lot step must be positive")
    return (qty / step).to_integral_value(rounding="ROUND_DOWN") * step


def calculate_size(data: SizingInput) -> SizingResult:
    """Full calculation following the algorithm of spec §4.

    The order of the constraints is part of the contract: the strictest one
    fires, and the reason code must point to the real obstacle.
    """
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        try:
            return _calculate(data)
        except (InvalidOperation, DivisionByZero, ValueError, OverflowError) as exc:
            # Extreme data (the LUNA collapse, the FTT crash) must not
            # bring the run down: the trade is rejected with an explicit
            # reason, and the engine keeps going.
            return SizingResult(
                approved=False,
                rejection=RejectionCode.EXCHANGE_ERROR,
                note=f"arithmetic failed: {type(exc).__name__}: {exc}",
            )


def _calculate(data: SizingInput) -> SizingResult:
    if data.equity <= 0:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.INSUFFICIENT_BALANCE,
            note=f"equity {data.equity}",
        )

    dist = stop_distance(data.entry_price, data.stop_price)
    if dist <= 0:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.LIQ_BUFFER,
            note="stop equals the entry price",
        )

    # 1. Margin per trade.
    margin = data.equity * data.margin_pct / 100

    # 2-3. Leverage is COMPUTED from the distance to the stop; the cap only
    # bounds it from above.
    l_max = max_leverage_for_stop(dist, data.mmr, data.min_liq_buffer_ratio)
    if l_max is None:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.LIQ_BUFFER,
            note=(
                f"stop {dist * 100:.2f}% is too far: even leverage 1 "
                f"does not give a {data.min_liq_buffer_ratio}x buffer at MMR {data.mmr}"
            ),
            stop_distance_pct=dist * 100,
        )

    leverage = min(l_max, data.leverage_cap)

    # 4. Notional.
    notional = margin * leverage

    # 5-6. Loss cap at the stop.
    loss = notional * dist
    max_loss = data.equity * data.max_loss_pct / 100
    if loss > max_loss:
        notional = max_loss / dist
        leverage = notional / margin if margin > 0 else Decimal(0)
        loss = notional * dist

    # 6a. Position size cap (DEC-040). The position is trimmed, not
    # rejected: with a close stop the loss cap allows a large leveraged
    # position, and that is exactly what this step cuts. Trimming can
    # only reduce the loss at the stop.
    if data.max_position_pct is not None:
        cap = data.equity * data.max_position_pct / 100
        if notional > cap:
            notional = cap
            leverage = notional / margin if margin > 0 else Decimal(0)
            loss = notional * dist

    if leverage <= 0:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.LIQ_BUFFER,
            note="leverage degenerated to zero",
            stop_distance_pct=dist * 100,
        )

    # 7. Round to the lot step, downward.
    raw_qty = notional / data.entry_price
    qty = round_qty_down(raw_qty, data.qty_step)
    if qty < data.min_order_qty:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.MIN_NOTIONAL,
            note=(
                f"after rounding {qty} < minimum lot "
                f"{data.min_order_qty}"
            ),
            stop_distance_pct=dist * 100,
        )

    notional = qty * data.entry_price

    # 8. The symbol's minimum notional.
    if notional < data.min_notional:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.MIN_NOTIONAL,
            note=f"notional {notional:.2f} < minimum {data.min_notional}",
            stop_distance_pct=dist * 100,
        )

    # Recompute after rounding: rounding down shrinks the position,
    # so leverage and loss must be the actual ones, not the computed ones.
    leverage = notional / margin
    loss = notional * dist

    liq = liquidation_price(
        data.entry_price, leverage, data.mmr, is_long=data.is_long
    )
    liq_dist = abs(data.entry_price - liq) / data.entry_price
    buffer = liq_dist / dist if dist > 0 else Decimal(0)

    # Final buffer check on the ACTUAL leverage. Rounding may have changed
    # it slightly, and the pre-rounding calculation cannot be relied on.
    if buffer < data.min_liq_buffer_ratio:
        return SizingResult(
            approved=False,
            rejection=RejectionCode.LIQ_BUFFER,
            note=f"buffer {buffer:.2f}x < required {data.min_liq_buffer_ratio}x",
            stop_distance_pct=dist * 100,
            liq_buffer_ratio=buffer,
        )

    return SizingResult(
        approved=True,
        qty=qty,
        notional=notional,
        margin=margin,
        leverage=leverage,
        stop_distance_pct=dist * 100,
        loss_at_stop=loss,
        liq_price=liq,
        liq_distance_pct=liq_dist * 100,
        liq_buffer_ratio=buffer,
    )
