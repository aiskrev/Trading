"""Decoder for the generic JSON quote-stream format. One decoder per vendor wire format.

Messages (one JSON object each):
  {"type": "quote", "seq": 12, "symbol": "AAA.US", "ts": "<ISO-8601 with offset>",
   "bid": "99.99", "ask": "100.01", "bid_size": "500", "ask_size": "500", "last": "100.00",
   "snapshot": false}
  {"type": "heartbeat", "seq": 13, "ts": "..."}
  {"type": "error", "code": "RATE_LIMIT", "retry_after_s": 5}
"""
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import Literal

from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.errors import AuthRejected, RateLimited, UnknownInstrument
from trading_agent.market_data.instruments import InstrumentRegistry
from trading_agent.market_data.records import QuoteRecord
from trading_agent.market_data.wire import DEFAULT_MAX_BYTES, DecodeError, strict_json_object


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _WireQuote(_Wire):
    type: Literal["quote"]
    seq: Optional[int] = Field(default=None, ge=0)
    symbol: str = Field(max_length=64)
    ts: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    last: Decimal
    snapshot: bool = False


class _WireHeartbeat(_Wire):
    type: Literal["heartbeat"]
    seq: Optional[int] = Field(default=None, ge=0)
    ts: datetime


class _WireError(_Wire):
    type: Literal["error"]
    code: str = Field(max_length=64, pattern=r"^[A-Z_]{1,64}$")
    retry_after_s: Optional[float] = Field(default=None, ge=0, le=3600)


_TYPES = {"quote": _WireQuote, "heartbeat": _WireHeartbeat, "error": _WireError}


class Decoded(NamedTuple):
    kind: str                       # "quote" | "heartbeat" | "error"
    seq: Optional[int]
    record: Optional[QuoteRecord] = None
    error_code: Optional[str] = None


class GenericJsonQuoteDecoder:
    def __init__(self, source_id: str, registry: InstrumentRegistry, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.source_id = source_id
        self.registry = registry
        self.max_bytes = max_bytes

    def decode(self, raw: bytes, received_at: datetime) -> Decoded:
        data = strict_json_object(raw, self.max_bytes)
        model = _TYPES.get(data.get("type")) if isinstance(data.get("type"), str) else None
        if model is None:
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "unknown or missing message type")
        try:
            msg = model.model_validate(data)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"schema invalid: {','.join(fields)[:120]}") from None

        if isinstance(msg, _WireError):
            if msg.code == "RATE_LIMIT":
                raise RateLimited(msg.retry_after_s)
            if msg.code in ("AUTH", "UNAUTHORIZED", "FORBIDDEN"):
                raise AuthRejected(msg.code)
            return Decoded("error", None, error_code=msg.code)
        if msg.ts.tzinfo is None:
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "timestamp without timezone")
        if isinstance(msg, _WireHeartbeat):
            return Decoded("heartbeat", msg.seq)

        try:
            instrument = self.registry.normalize(self.source_id, msg.symbol)
        except UnknownInstrument:
            raise DecodeError(DataIssueCode.UNKNOWN_INSTRUMENT, "symbol not in instrument registry") from None
        try:
            record = QuoteRecord(source_id=self.source_id, instrument=instrument, vendor_symbol=msg.symbol.strip(),
                                 seq=msg.seq, source_ts=msg.ts, received_at=received_at, bid=msg.bid, ask=msg.ask,
                                 bid_size=msg.bid_size, ask_size=msg.ask_size, last=msg.last,
                                 is_snapshot=msg.snapshot)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"invalid values: {','.join(fields)[:120]}") from None
        return Decoded("quote", msg.seq, record)
