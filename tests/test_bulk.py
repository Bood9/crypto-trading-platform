"""Tests for batched writes.

Regression: the mass backfill crashed on the very first symbol. Two years
of 15m candle history is 70 thousand rows of 8 columns, i.e. 561 thousand
bind parameters against asyncpg's hard limit of 32767.
"""

from __future__ import annotations

import pytest

from src.core.bulk import MAX_QUERY_PARAMS, chunk_size_for, chunks


def test_chunk_size_stays_under_driver_limit():
    for columns in range(1, 60):
        size = chunk_size_for(columns)
        assert size * columns <= MAX_QUERY_PARAMS


def test_chunk_size_leaves_headroom():
    """SQLAlchemy adds its own parameters in ON CONFLICT.

    Hitting the ceiling exactly at the boundary is easy, so we keep a margin.
    """
    columns = 8
    assert chunk_size_for(columns) * columns < MAX_QUERY_PARAMS * 0.9


def test_ohlcv_two_year_history_is_split():
    """The exact case where everything broke."""
    rows_in_two_years = 70_128
    size = chunk_size_for(8)  # columns in ohlcv

    assert size < rows_in_two_years
    batches = list(chunks([{}] * rows_in_two_years, size))
    assert len(batches) > 1
    assert sum(len(b) for b in batches) == rows_in_two_years


def test_wide_table_still_gets_at_least_one_row():
    """A table wider than the limit would otherwise get zero rows per batch
    and silently write nothing."""
    # More columns than parameters fit: the division yields zero.
    assert chunk_size_for(50_000) == 1


def test_chunk_size_never_returns_zero():
    for columns in (1, 8, 100, 10_000, 32_768, 1_000_000):
        assert chunk_size_for(columns) >= 1


def test_zero_columns_is_rejected():
    with pytest.raises(ValueError):
        chunk_size_for(0)


def test_chunks_preserve_order_and_content():
    rows = [{"i": i} for i in range(25)]
    flattened = [r for batch in chunks(rows, 10) for r in batch]
    assert flattened == rows


def test_chunks_on_empty_input():
    assert list(chunks([], 10)) == []


def test_single_chunk_when_rows_fit():
    rows = [{"i": i} for i in range(5)]
    assert len(list(chunks(rows, 100))) == 1
