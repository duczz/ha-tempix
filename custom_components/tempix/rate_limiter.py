"""Evaluation rate limiter (V2, companion to the Single-Slot Mailbox).

Throttles how often the engine may evaluate (default: max 5 evaluations
per second). Hard rule: the limiter must NEVER discard
an event — it only delays execution. Collapsing bursts down to the
newest state is NOT this module's job; that is what the Single-Slot
Latest-State Mailbox does on top of it.

Deterministic by construction (same idea as the explicit time model in
``clock.py``): both the monotonic time source and the sleep function are
injectable, so tests never depend on wall-clock time. The mailbox loop
(``DecisionEngine.submit()``) awaits it when one is passed in; the
production cycle calls ``DecisionEngine.calculate()`` directly and
creates no limiter.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

DEFAULT_MAX_EVALUATIONS_PER_SECOND = 5.0

# Residual waits below this count as "slot free". Guards against a
# float-absorption spin: the remaining delay can end up smaller than the
# clock's float resolution (e.g. 2.8e-17 s at now=0.8), so sleeping it
# advances time by exactly 0.0 and the re-check loop in acquire() would
# never terminate. 1 µs is noise against the 200 ms default interval.
_TIMING_EPSILON = 1e-6


class EvaluationRateLimiter:
    """Delay-only throttle for engine evaluations (one instance per room).

    ``acquire()`` blocks until the next evaluation slot is free, then
    claims it. Concurrent callers are serialized via an internal lock so
    two evaluations can never share one slot — every caller eventually
    gets a slot (nothing is dropped).
    """

    def __init__(
        self,
        max_per_second: float = DEFAULT_MAX_EVALUATIONS_PER_SECOND,
        *,
        time_source: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_per_second <= 0:
            raise ValueError(f"max_per_second must be > 0, got {max_per_second}")
        self._min_interval = 1.0 / max_per_second
        self._time = time_source
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_slot: float | None = None

    @property
    def min_interval(self) -> float:
        """Minimum spacing between two evaluation slots in seconds."""
        return self._min_interval

    def seconds_until_next_slot(self) -> float:
        """Return how long the next ``acquire()`` would wait right now."""
        if self._last_slot is None:
            return 0.0
        elapsed = self._time() - self._last_slot
        remaining = self._min_interval - elapsed
        return remaining if remaining > _TIMING_EPSILON else 0.0

    async def acquire(self) -> None:
        """Wait for the next free evaluation slot and claim it.

        Never raises, never skips a caller — a burst of N callers takes
        at least ``(N - 1) * min_interval`` seconds but all N run.
        """
        async with self._lock:
            # Loop: the injected time source may advance while sleeping
            # less than requested (coarse timers), so re-check the slot.
            while (delay := self.seconds_until_next_slot()) > 0:
                await self._sleep(delay)
            self._last_slot = self._time()

    def reset(self) -> None:
        """Forget the last slot (e.g. on config reload) — next acquire is immediate."""
        self._last_slot = None
