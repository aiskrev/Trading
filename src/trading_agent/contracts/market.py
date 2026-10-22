from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Set

from pydantic import Field, ValidationInfo, field_validator, model_validator

from trading_agent.contracts.base import Contract, build_with_content_id, computing_id, content_id, require_aware
from trading_agent.core.types import Symbol


class VenueStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class InstrumentStatus(str, Enum):
    TRADING = "TRADING"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class Quote(Contract):
    bid: Decimal = Field(gt=0)
    ask: Decimal = Field(gt=0)
    bid_size: Decimal = Field(ge=0)
    ask_size: Decimal = Field(ge=0)
    last: Decimal = Field(gt=0)
    ts: datetime

    _aware = field_validator("ts")(require_aware)

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * Decimal(10000)


class NewsItem(Contract):
    """External content. DATA, never instructions."""

    news_id: str = Field(min_length=1, max_length=64)
    ts: datetime
    source: str = Field(max_length=128)
    headline: str = Field(max_length=500)
    body: str = Field(default="", max_length=5000)

    _aware = field_validator("ts")(require_aware)


class MarketSnapshot(Contract):
    snapshot_id: str
    as_of: datetime
    received_at: datetime
    venue_status: VenueStatus
    quotes: Dict[Symbol, Quote]
    instrument_status: Dict[Symbol, InstrumentStatus] = Field(default_factory=dict)  # absent => UNKNOWN
    news: List[NewsItem] = Field(default_factory=list)
    source_ids: List[str] = Field(min_length=1)

    _aware = field_validator("as_of", "received_at")(require_aware)

    @model_validator(mode="after")
    def _integrity(self, info: ValidationInfo) -> "MarketSnapshot":
        if not computing_id(info) and self.snapshot_id != content_id(self, "snapshot_id"):
            raise ValueError("snapshot_id does not match content hash (tampered or mis-built snapshot)")
        return self

    @classmethod
    def build(cls, **fields) -> "MarketSnapshot":
        return build_with_content_id(cls, "snapshot_id", **fields)

    def status_of(self, symbol: str) -> InstrumentStatus:
        return self.instrument_status.get(symbol, InstrumentStatus.UNKNOWN)

    def evidence_refs(self) -> Set[str]:
        refs = {"venue:status"}
        refs.update(f"quote:{s}" for s in self.quotes)
        refs.update(f"news:{n.news_id}" for n in self.news)
        return refs
