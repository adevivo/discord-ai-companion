"""In-memory sliding-window rate limiter, keyed (e.g. per user, or one global key)."""

import time
from collections import defaultdict, deque
from collections.abc import Callable


class RateLimiter:
    def __init__(
        self,
        max_requests: int,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        self._max = max_requests
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def retry_after(self, key: str) -> float:
        """Seconds until `key` may make a request (0 if allowed now). Records nothing."""
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._max:
            return self._window - (now - hits[0])
        return 0.0

    def record(self, key: str) -> None:
        self._hits[key].append(self._clock())

    def try_acquire(self, key: str) -> float:
        """Record a request if allowed and return 0; otherwise return seconds until allowed."""
        wait = self.retry_after(key)
        if wait == 0:
            self.record(key)
        return wait
