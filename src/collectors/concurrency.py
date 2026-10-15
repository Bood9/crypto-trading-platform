"""Bounded concurrency for bulk loading.

The backfill ran strictly sequentially: symbol after symbol, stream after
stream. Measured: about 290 requests per symbol at an actual 2.5
requests per second — against a limit of 20. Each request waited for the
previous one, and nine tenths of the throughput sat idle waiting on the network.

SYMBOLS run in parallel, not the streams within a symbol. Checkpoints of a
(symbol, stream) pair are independent, so symbols share nothing;
and the streams of one symbol are easier kept sequential — the report
on a symbol reads as a whole.

Safety does not live here but in three things, tested separately:

* the token bucket under `asyncio.Lock` — the total rate never exceeds the
  limit with any number of tasks;
* the circuit breaker — shared client state: once tripped in one task,
  it stops all the others on their very next request;
* writes are idempotent by key (symbol, time): an interrupted batch
  rolls back, the checkpoint does not move, a retry downloads it again.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def bounded(
    items: Iterable[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int,
    stop: Callable[[], bool] = lambda: False,
) -> list[R]:
    """Run `worker` over all items, at most `limit` at a time.

    `stop` is checked BEFORE starting each task: once the circuit breaker
    trips, no new symbols start, and those already started reach
    their next request and get refused by the client themselves.

    Results are returned in COMPLETION order, not input order: the load
    report is interesting as it becomes ready, and there is no point
    waiting for a slow symbol to print a fast one.
    """
    if limit < 1:
        raise ValueError(f"concurrency limit must be positive: {limit}")

    gate = asyncio.Semaphore(limit)
    results: list[R] = []

    async def guarded(item: T) -> None:
        async with gate:
            if stop():
                return
            results.append(await worker(item))

    await asyncio.gather(*(guarded(item) for item in items))
    return results


__all__ = ["bounded"]
