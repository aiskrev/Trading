from decimal import Decimal
from typing import List, Optional, Sequence

from typing_extensions import Protocol

from trading_agent.contracts.market import Quote

Q = Decimal("0.000001")
BPS = Decimal(10000)


class Feature(Protocol):
    name: str

    def compute(self, history: Sequence[Quote]) -> Optional[Decimal]:
        """history is oldest -> newest and includes the current quote. None = not enough data."""


class SpreadBps:
    name = "spread_bps"

    def compute(self, history: Sequence[Quote]) -> Optional[Decimal]:
        return history[-1].spread_bps.quantize(Q)


class ReturnBps:
    def __init__(self, lookback: int) -> None:
        self.lookback = lookback
        self.name = f"return_{lookback}_bps"

    def compute(self, history: Sequence[Quote]) -> Optional[Decimal]:
        if len(history) <= self.lookback:
            return None
        then, now = history[-1 - self.lookback].mid, history[-1].mid
        return ((now / then - 1) * BPS).quantize(Q)


class DeviationFromSmaBps:
    def __init__(self, window: int) -> None:
        self.window = window
        self.name = f"dev_from_sma_{window}_bps"

    def compute(self, history: Sequence[Quote]) -> Optional[Decimal]:
        if len(history) < self.window:
            return None
        mids = [q.mid for q in history[-self.window:]]
        sma = sum(mids) / len(mids)
        return ((history[-1].mid / sma - 1) * BPS).quantize(Q)


def default_features() -> List[Feature]:
    return [SpreadBps(), ReturnBps(5), DeviationFromSmaBps(10)]
