"""Token bucket for requests to Bybit.

Spec §13: exceeding the limit leads to an IP block, not a soft rejection,
so we keep the rate well below the documented ceiling and never rely on
the exchange's response as the signal "time to slow down".

The clock is injected so that tests do not depend on real time.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable

Clock = Callable[[], float]
Sleeper = Callable[[float], object]

# Tolerance for accumulated binary fraction error. Tokens are computed by
# multiplying a fractional interval by the rate, so after a series of grants
# tokens arrives at 0.9999999999999998 instead of 1.0. Without the tolerance
# a 2e-16 deficit produces a 1e-17 delay that does not move time at all —
# the bucket goes into a busy loop and lets no request through.
_EPSILON = 1e-9

# Delay floor: guarantees time advances even if the tolerance above
# somehow did not work.
_MIN_WAIT = 1e-6


class TokenBucket:
    """Classic token bucket.

    `rate` tokens per second, capacity `burst`. `acquire()` waits until a
    token appears. Implemented without background refilling: tokens are
    computed lazily from elapsed time, so an idle process does not
    accumulate debt beyond the capacity.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")

        self._rate = rate
        self._burst = float(burst)
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._tokens = float(burst)
        self._updated = clock()
        self._lock = asyncio.Lock()

    @property
    def tokens(self) -> float:
        return self._tokens

    def _refill(self) -> None:
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._updated = now

    async def acquire(self, amount: float = 1.0) -> None:
        if amount > self._burst:
            raise ValueError(
                f"requested {amount} tokens with capacity {self._burst}: "
                "the request will never be satisfied"
            )
        # A loop, not a single check: another coroutine may squeeze in
        # between waking up and taking the token.
        while True:
            async with self._lock:
                self._refill()
                if self._tokens + _EPSILON >= amount:
                    # A negative remainder is impossible: the tolerance eats
                    # only fractional dust, not a real deficit.
                    self._tokens = max(0.0, self._tokens - amount)
                    return
                deficit = amount - self._tokens
                wait = max(deficit / self._rate, _MIN_WAIT)
            await self._sleep(wait)


class Backoff:
    """Exponential delay with jitter.

    Jitter is mandatory: without it parallel collectors retry in sync after
    a common failure and create the very burst that caused the failure in
    the first place.
    """

    def __init__(
        self,
        base_seconds: float,
        max_seconds: float,
        *,
        rng: random.Random | None = None,
    ) -> None:
        if base_seconds <= 0 or max_seconds <= 0:
            raise ValueError("base_seconds and max_seconds must be positive")
        self._base = base_seconds
        self._max = max_seconds
        self._rng = rng or random.Random()

    def delay(self, attempt: int) -> float:
        """Delay before attempt `attempt` (zero-based).

        The jitter is half, not full. Full jitter (`uniform(0, raw)`) could
        return almost zero and send the retry instantly — i.e. disable
        backoff exactly on the rate-limit error it exists for.
        Half the interval is guaranteed, the other half is random: the spread
        is preserved, and so is the minimum pause.
        """
        if attempt < 0:
            raise ValueError("attempt cannot be negative")
        # Cap the exponent before raising to the power: with a large
        # attempt 2**attempt overflows float before min() kicks in.
        capped = min(attempt, 32)
        raw = min(self._base * (2**capped), self._max)
        half = raw / 2
        return half + self._rng.uniform(0.0, half)
