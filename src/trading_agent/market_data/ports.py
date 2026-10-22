from datetime import datetime
from decimal import Decimal
from typing import Iterator

from pydantic import field_validator
from typing_extensions import Protocol

from trading_agent.contracts.base import Contract, require_aware


class RawTick(Contract):
    """Unvalidated vendor data. Prices may be zero, crossed or wrong; the normalizer decides."""

    source_id: str
    symbol: str
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    last: Decimal

    _aware = field_validator("ts")(require_aware)


class MarketDataSource(Protocol):
    source_id: str

    def ticks(self) -> Iterator[RawTick]: ...
