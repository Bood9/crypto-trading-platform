"""Token bucket and backoff tests.

The clock is fake: a test that depends on real time is either slow
or flaky.
"""

from __future__ import annotations

import random

import pytest

from src.exchange.rate_limiter import Backoff, TokenBucket


class FakeClock:
    """A clock that only sleep moves."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_bucket(rate: float, burst: int) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    return TokenBucket(rate, burst, clock=clock, sleep=clock.sleep), clock


async def test_burst_passes_without_waiting():
    bucket, clock = make_bucket(rate=10, burst=5)
    for _ in range(5):
        await bucket.acquire()
    assert clock.slept == []


async def test_waits_once_bucket_is_drained():
    bucket, clock = make_bucket(rate=10, burst=2)
    await bucket.acquire()
    await bucket.acquire()

    await bucket.acquire()

    # The third request waits exactly one token at a rate of 10/s.
    assert clock.slept == [pytest.approx(0.1)]
    assert clock.now == pytest.approx(0.1)


async def test_refills_over_time_up_to_capacity():
    bucket, clock = make_bucket(rate=10, burst=5)
    for _ in range(5):
        await bucket.acquire()

    # Idle for longer than a full refill needs.
    clock.now += 100.0
    await bucket.acquire()

    # Credit does not accumulate beyond capacity: after idling, a burst is available,
    # not a hundred seconds of accrual.
    assert bucket.tokens == pytest.approx(4.0)
    assert clock.slept == []


async def test_sustained_rate_matches_configuration():
    bucket, clock = make_bucket(rate=20, burst=1)
    for _ in range(21):
        await bucket.acquire()

    # 1 request from the bucket + 20 at 0.05s = exactly one second for 20 requests.
    assert clock.now == pytest.approx(1.0, abs=1e-6)


async def test_long_run_does_not_livelock():
    """Regression: accumulated float error drove the bucket into a busy-loop.

    tokens came to 0.9999999999999998, the deficit was 2e-16, the delay
    1e-17, and `now + wait == now`: time did not move, requests did not pass.
    Meanwhile the process looked alive and burned CPU.

    Reproduced on the ninth grant with rate=20, burst=1.
    """
    bucket, clock = make_bucket(rate=20, burst=1)

    for _ in range(500):
        await bucket.acquire()

    # Every iteration must move time forward.
    assert clock.now == pytest.approx(499 / 20, rel=1e-6)
    assert all(w > 0 for w in clock.slept)


@pytest.mark.parametrize("rate,burst", [(20, 1), (3, 7), (0.5, 2), (1000, 50)])
async def test_no_livelock_across_configurations(rate, burst):
    bucket, clock = make_bucket(rate=rate, burst=burst)
    for _ in range(200):
        await bucket.acquire()
    assert clock.now >= 0


async def test_tokens_never_go_negative():
    """The tolerance must not allow going negative on tokens."""
    bucket, _ = make_bucket(rate=10, burst=3)
    for _ in range(50):
        await bucket.acquire()
        assert bucket.tokens >= 0.0


async def test_acquire_more_than_capacity_is_rejected():
    bucket, _ = make_bucket(rate=10, burst=5)
    with pytest.raises(ValueError, match="will never be satisfied"):
        await bucket.acquire(6)


def test_invalid_configuration_is_rejected():
    with pytest.raises(ValueError):
        TokenBucket(0, 5)
    with pytest.raises(ValueError):
        TokenBucket(10, 0)


def test_backoff_grows_exponentially_and_is_capped():
    # An rng that always returns the upper bound: we check the ceiling itself,
    # not the jitter.
    rng = random.Random()
    rng.uniform = lambda a, b: b  # type: ignore[method-assign]
    backoff = Backoff(base_seconds=0.5, max_seconds=8.0, rng=rng)

    assert [backoff.delay(i) for i in range(6)] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]


def test_backoff_never_returns_near_zero():
    """Full jitter could send a retry instantly.

    On a rate limit error that disabled backoff exactly where it is needed:
    the retry hit the same limit, burned an attempt and brought an IP ban closer.
    """
    rng = random.Random()
    rng.uniform = lambda a, b: 0.0  # worst case of jitter  # type: ignore[method-assign]
    backoff = Backoff(base_seconds=0.5, max_seconds=8.0, rng=rng)

    # Half of the computed interval is guaranteed.
    assert [backoff.delay(i) for i in range(5)] == [0.25, 0.5, 1.0, 2.0, 4.0]


def test_backoff_still_spreads_retries():
    """The spread must be preserved: without it parallel collectors
    hit the exchange in sync after a shared failure and reproduce the rejection."""
    backoff = Backoff(base_seconds=1.0, max_seconds=60.0, rng=random.Random(7))
    delays = {backoff.delay(3) for _ in range(50)}
    assert len(delays) > 40


def test_backoff_survives_large_attempt_numbers():
    """2**attempt overflows float before min() kicks in."""
    backoff = Backoff(base_seconds=0.5, max_seconds=8.0)
    assert backoff.delay(10_000) <= 8.0


def test_backoff_jitter_stays_within_bounds():
    backoff = Backoff(base_seconds=1.0, max_seconds=10.0, rng=random.Random(42))
    for attempt in range(8):
        delay = backoff.delay(attempt)
        assert 0.0 <= delay <= 10.0


def test_backoff_rejects_negative_attempt():
    backoff = Backoff(base_seconds=1.0, max_seconds=10.0)
    with pytest.raises(ValueError):
        backoff.delay(-1)
