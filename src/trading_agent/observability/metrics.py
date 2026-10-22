from collections import Counter
from typing import Dict


class Metrics:
    """In-process counters. Swap for Prometheus/OTel exporters later behind the same interface."""

    def __init__(self) -> None:
        self._counters: Counter = Counter()

    def inc(self, name: str, value: int = 1) -> None:
        self._counters[name] += value

    def snapshot(self) -> Dict[str, int]:
        return dict(sorted(self._counters.items()))
