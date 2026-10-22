import random
from typing import Optional

MAX_EXPONENT = 64


class ExponentialBackoff:
    """Exponential backoff with "equal jitter": delay in [cap/2, cap], cap = min(max_s, base_s * factor**n).

    Half the delay is guaranteed (no reconnect storms at ~0s); the other half is random so many
    clients do not reconnect in lockstep. Pass a seeded `rng` for deterministic tests.
    """

    def __init__(self, base_s: float = 0.5, factor: float = 2.0, max_s: float = 30.0,
                 rng: Optional[random.Random] = None) -> None:
        if base_s <= 0 or factor < 1 or max_s < base_s:
            raise ValueError("require base_s > 0, factor >= 1, max_s >= base_s")
        self.base_s, self.factor, self.max_s = base_s, factor, max_s
        self._rng = rng or random.Random()
        self._attempt = 0

    @property
    def attempts(self) -> int:
        return self._attempt

    def cap(self, attempt: int) -> float:
        return min(self.max_s, self.base_s * self.factor ** min(attempt, MAX_EXPONENT))

    def next_delay(self) -> float:
        cap = self.cap(self._attempt)
        self._attempt += 1
        return cap / 2 + self._rng.uniform(0, cap / 2)

    def reset(self) -> None:
        self._attempt = 0
