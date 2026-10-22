from decimal import Decimal
from enum import Enum

from pydantic import Field
from typing_extensions import Annotated

Symbol = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9.\-]{0,14}$")]
Money = Decimal
EvidenceRef = Annotated[str, Field(min_length=3, max_length=128)]


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class IdFactory:
    """Deterministic, run-scoped IDs so replays produce identical audit ledgers."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self._counters = {}

    def next(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{self._run_id}:{prefix}:{n:08d}"
