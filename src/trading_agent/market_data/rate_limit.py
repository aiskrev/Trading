from trading_agent.core.clock import Clock


class TokenBucket:
    """Client-side request limiter so we stay under the vendor's published rate limit."""

    def __init__(self, rate_per_s: float, capacity: int, clock: Clock) -> None:
        if rate_per_s <= 0 or capacity < 1:
            raise ValueError("require rate_per_s > 0 and capacity >= 1")
        self.rate, self.capacity, self._clock = rate_per_s, capacity, clock
        self._tokens = float(capacity)
        self._last = clock.now()

    def try_acquire(self) -> float:
        """Take one token. Returns 0.0 on success, else seconds to wait (nothing is taken)."""
        now = self._clock.now()
        elapsed = max(0.0, (now - self._last).total_seconds())
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self.rate
