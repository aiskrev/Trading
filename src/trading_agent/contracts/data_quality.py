from enum import Enum
from typing import Optional

from pydantic import Field

from trading_agent.contracts.base import Contract


class DataIssueCode(str, Enum):
    # normalization (snapshot build)
    BAD_PRICE = "BAD_PRICE"
    CROSSED_QUOTE = "CROSSED_QUOTE"
    SOURCE_DIVERGENCE = "SOURCE_DIVERGENCE"
    MISSING_PRIMARY = "MISSING_PRIMARY"
    CLOCK_SKEW = "CLOCK_SKEW"  # our clock vs. snapshot: HALT
    BAD_SYMBOL = "BAD_SYMBOL"
    # feed / adapter level
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"  # vendor timestamp ahead of our clock
    LATE_MESSAGE = "LATE_MESSAGE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DUPLICATE_MESSAGE = "DUPLICATE_MESSAGE"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    STALE_INSTRUMENT = "STALE_INSTRUMENT"
    STALE_FEED = "STALE_FEED"
    DISCONNECTED = "DISCONNECTED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE"
    AUTH_REJECTED = "AUTH_REJECTED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    ACCOUNT_STALE = "ACCOUNT_STALE"


class DataIssue(Contract):
    """A data-quality event. `detail` is diagnostic only and never contains raw payloads or secrets."""

    code: DataIssueCode
    symbol: Optional[str] = None
    detail: str = Field(default="", max_length=500)
    source_id: Optional[str] = None
