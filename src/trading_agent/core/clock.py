from datetime import datetime, timedelta, timezone

from typing_extensions import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """UTC wall clock for adapters outside BACKTEST. Audit timestamps in BACKTEST never use it."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SimulatedClock:
    """Backtest clock. Only moves forward; audit timestamps come from here, never wall time."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("clock requires timezone-aware datetimes")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance_to(self, t: datetime) -> None:
        if t < self._now:
            raise ValueError(f"clock cannot move backwards: {t} < {self._now}")
        self._now = t

    def advance(self, seconds: float) -> datetime:
        self.advance_to(self._now + timedelta(seconds=seconds))
        return self._now
