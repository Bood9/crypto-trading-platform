"""Bounded concurrency of the mass download.

Checks the ceiling on simultaneous tasks and that no new tasks start after
the circuit breaker trips. Both bugs look
harmless: the first turns into an IP ban, the second into a thousand requests
to an exchange that has already said "enough".
"""

from __future__ import annotations

import asyncio

import pytest

from src.collectors.concurrency import bounded


@pytest.mark.asyncio
async def test_never_exceeds_the_limit():
    running = 0
    peak = 0

    async def worker(i: int) -> int:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.001)
        running -= 1
        return i

    result = await bounded(range(40), worker, limit=6)
    assert sorted(result) == list(range(40))
    assert peak == 6


@pytest.mark.asyncio
async def test_limit_of_one_is_strictly_sequential():
    order: list[str] = []

    async def worker(i: int) -> None:
        order.append(f"start {i}")
        await asyncio.sleep(0)
        order.append(f"end {i}")

    await bounded(range(3), worker, limit=1)
    assert order == ["start 0", "end 0", "start 1", "end 1", "start 2", "end 2"]


@pytest.mark.asyncio
async def test_stop_prevents_new_tasks_from_starting():
    """After the circuit breaker trips, no new symbols are started."""
    started: list[int] = []
    tripped = False

    async def worker(i: int) -> int:
        nonlocal tripped
        started.append(i)
        if i == 2:
            tripped = True
        await asyncio.sleep(0.001)
        return i

    await bounded(range(20), worker, limit=1, stop=lambda: tripped)
    assert started == [0, 1, 2]


@pytest.mark.asyncio
async def test_empty_input_is_not_an_error():
    async def worker(i: int) -> int:
        return i

    assert await bounded([], worker, limit=4) == []


@pytest.mark.asyncio
async def test_non_positive_limit_is_refused():
    async def worker(i: int) -> int:
        return i

    with pytest.raises(ValueError):
        await bounded([1], worker, limit=0)
