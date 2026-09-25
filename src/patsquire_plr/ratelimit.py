"""Request rate limiting (token bucket, thread-safe, in-process), shared by the model
gateway and the patent data sources."""

from __future__ import annotations

import threading
from collections.abc import Callable

SECONDS_PER_MINUTE = 60.0


class RateLimiter:
    """Allows ``requests_per_minute`` on average, with bursts up to that number."""

    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        if requests_per_minute < 1:
            raise ValueError("requests_per_minute must be >= 1")
        self._capacity = float(requests_per_minute)
        self._rate = requests_per_minute / SECONDS_PER_MINUTE
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a request may be sent. Returns the seconds spent waiting."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return waited
                wait = (1 - self._tokens) / self._rate
            self._sleep(wait)
            waited += wait
