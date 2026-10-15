"""Storing instrument parameters in the DB.

The cache is not for speed but for honesty: spec §16 forbids hardcoding
min_notional, qty_step and MMR, and `updated_at` shows how fresh the values
were at the decision moment. The exchange changes parameters without
warning.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from src.core.bulk import chunk_size_for, chunks
from src.core.db import Instrument, session_scope
from src.core.logging import get_logger
from src.core.models import InstrumentSpec

log = get_logger(__name__)

_UPDATABLE = (
    "base_coin",
    "quote_coin",
    "contract_type",
    "status",
    "symbol_type",
    "tick_size",
    "qty_step",
    "min_order_qty",
    "max_order_qty",
    "min_notional_value",
    "max_leverage",
    "funding_interval_minutes",
    "launch_time",
    "mmr",
    "initial_margin_rate",
    "risk_limit_value",
    "is_delisted",
)

#: Risk fields come from a separate request (`enrich_mmr`) and not for every
#: symbol. An empty value in the response means "not asked", not "no MMR":
#: it must not overwrite the stored one (the nightly collection writes specs without MMR).
_KEEP_IF_EMPTY = frozenset({"mmr", "initial_margin_rate", "risk_limit_value"})


def _row(spec: InstrumentSpec) -> dict:
    return {
        "symbol": spec.symbol,
        "base_coin": spec.base_coin,
        "quote_coin": spec.quote_coin,
        "contract_type": spec.contract_type,
        "status": spec.status,
        "symbol_type": spec.symbol_type,
        "tick_size": spec.tick_size,
        "qty_step": spec.qty_step,
        "min_order_qty": spec.min_order_qty,
        "max_order_qty": spec.max_order_qty,
        "min_notional_value": spec.min_notional_value,
        "max_leverage": spec.max_leverage,
        "funding_interval_minutes": spec.funding_interval_minutes,
        "launch_time": spec.launch_time,
        "mmr": spec.mmr,
        "initial_margin_rate": spec.initial_margin_rate,
        "risk_limit_value": spec.risk_limit_value,
        "is_delisted": spec.is_delisted,
    }


async def upsert_instruments(specs: list[InstrumentSpec]) -> int:
    """Idempotent batch write.

    Upsert, not delete+insert: deletion would zero the parameters of symbols
    temporarily missing from the exchange response, and the next position
    size computation would run on empty values.
    """
    if not specs:
        return 0

    now = datetime.now(UTC)
    rows = [_row(s) | {"updated_at": now} for s in specs]
    size = chunk_size_for(len(rows[0]))
    async with session_scope() as session:
        for batch in chunks(rows, size):
            stmt = insert(Instrument).values(list(batch))
            set_ = {
                c: (
                    func.coalesce(getattr(stmt.excluded, c), getattr(Instrument, c))
                    if c in _KEEP_IF_EMPTY
                    else getattr(stmt.excluded, c)
                )
                for c in _UPDATABLE
            }
            # Previously `updated_at` was not touched on update and forever
            # stayed the date of the first write (night re-check 2026-09-28).
            set_["updated_at"] = stmt.excluded.updated_at
            await session.execute(
                stmt.on_conflict_do_update(index_elements=[Instrument.symbol], set_=set_)
            )
    log.info("instruments.saved", count=len(rows))
    return len(rows)
