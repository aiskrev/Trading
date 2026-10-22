from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from pydantic import Field, field_validator

from trading_agent.contracts.base import Contract, require_aware
from trading_agent.core.types import Symbol
from trading_agent.market_data.ports import RawTick


class QuoteRecord(Contract):
    """One normalized quote. Always carries its source, the source's timestamp and our receive time."""

    source_id: str = Field(min_length=1, max_length=64)
    instrument: Symbol                     # canonical id from InstrumentRegistry
    vendor_symbol: str = Field(min_length=1, max_length=32)
    channel: str = Field(default="quotes", max_length=32)
    seq: Optional[int] = Field(default=None, ge=0)
    source_ts: datetime                    # exchange/vendor timestamp
    received_at: datetime                  # our clock at receipt
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    bid_size: Decimal = Field(ge=0)
    ask_size: Decimal = Field(ge=0)
    last: Decimal = Field(gt=0)
    is_snapshot: bool = False

    _aware = field_validator("source_ts", "received_at")(require_aware)

    @property
    def latency(self) -> timedelta:
        return self.received_at - self.source_ts

    def to_raw_tick(self) -> RawTick:
        return RawTick(source_id=self.source_id, symbol=self.instrument, ts=self.source_ts, bid=self.bid,
                       ask=self.ask, bid_size=self.bid_size, ask_size=self.ask_size, last=self.last)
