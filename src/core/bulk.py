"""Batched writes that respect the driver's parameter limit.

asyncpg rejects a query with more than 32767 bind parameters. For a table
of N columns this gives a ceiling of 32767 // N rows per INSERT — about
four thousand for candles with their eight columns.

Two years of 15m candles for one symbol is 70 thousand rows, i.e. a bulk
backfill hits the limit on the very first symbol. So batched writing here
is not an optimization but a precondition for working at all.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import insert

from src.core.db import session_scope
from src.core.logging import get_logger

log = get_logger(__name__)

#: Hard limit of the asyncpg protocol.
MAX_QUERY_PARAMS = 32767

#: Margin below the limit. SQLAlchemy adds its own parameters in
#: ON CONFLICT, and it is easy to hit the ceiling right at the boundary.
SAFETY_FACTOR = 0.8


def chunk_size_for(columns: int, *, max_params: int = MAX_QUERY_PARAMS) -> int:
    """How many rows fit in one query for a given number of columns."""
    if columns <= 0:
        raise ValueError("number of columns must be positive")
    size = int(max_params * SAFETY_FACTOR) // columns
    # At least one row: a table with hundreds of columns would otherwise give
    # zero and silently write nothing.
    return max(1, size)


def chunks(rows: Sequence[dict], size: int) -> Iterator[Sequence[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


async def upsert_chunked(
    table: Any,
    rows: Sequence[dict],
    *,
    index_elements: Sequence[Any],
    update_columns: Sequence[str] | None = None,
) -> int:
    """Idempotent write in batches.

    `update_columns=None` means DO NOTHING: the row already exists and there
    is nothing to overwrite it with (the liquidations case — there the key is
    surrogate and conflicts do not happen).

    All batches go in one transaction: a partially written window is worse
    than an unwritten one, because the checkpoint after it will move past the hole.
    """
    if not rows:
        return 0

    size = chunk_size_for(len(rows[0]))

    async with session_scope() as session:
        for batch in chunks(rows, size):
            stmt = insert(table).values(list(batch))
            if update_columns:
                stmt = stmt.on_conflict_do_update(
                    index_elements=list(index_elements),
                    set_={c: getattr(stmt.excluded, c) for c in update_columns},
                )
            else:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(index_elements))
            await session.execute(stmt)

    if len(rows) > size:
        log.debug("bulk.chunked", rows=len(rows), chunk_size=size)
    return len(rows)
