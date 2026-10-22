import time
from contextlib import contextmanager
from typing import Dict, Iterator, List


class Tracer:
    """Minimal span recorder (wall-clock durations are telemetry only, never audit data)."""

    def __init__(self) -> None:
        self.spans: List[Dict[str, object]] = []

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.spans.append({"name": name, "ms": round((time.perf_counter() - start) * 1000, 3)})
