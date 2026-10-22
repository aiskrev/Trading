"""Read-only REST account adapter for the generic JSON account format.

GET /v1/account ->
  {"as_of": "<ISO-8601 with offset>", "version": 41, "cash": "100000.00",
   "realized_pnl_today": "0", "orders_today": 0,
   "positions": [{"symbol": "AAA US EQUITY", "quantity": "10", "avg_price": "100.00"}],
   "open_orders": [{"order_ref": "o-1", "symbol": "AAA US EQUITY", "side": "BUY",
                    "notional": "1000", "limit_price": "99.50"}]}

All-or-nothing: if any part of the payload is invalid (unknown instrument, duplicate
position, bad number) the whole fetch is rejected. A partial account is never returned.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, NamedTuple, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing_extensions import Literal

from trading_agent.account_data.http import ReadOnlyHttpClient
from trading_agent.account_data.records import AccountRecord, OpenOrderRecord, PositionRecord
from trading_agent.contracts.data_quality import DataIssue, DataIssueCode
from trading_agent.core.clock import Clock
from trading_agent.core.errors import AuthRejected, RateLimited, SecretUnavailable, TransportError, UnknownInstrument
from trading_agent.market_data.backoff import ExponentialBackoff
from trading_agent.market_data.instruments import InstrumentRegistry
from trading_agent.market_data.wire import DecodeError, strict_json_object
from trading_agent.observability.logging import get_logger

ACCOUNT_PATH = "/v1/account"


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _WirePosition(_Wire):
    symbol: str = Field(max_length=64)
    quantity: Decimal
    avg_price: Decimal


class _WireOrder(_Wire):
    order_ref: str = Field(max_length=64)
    symbol: str = Field(max_length=64)
    side: Literal["BUY", "SELL"]
    notional: Decimal
    limit_price: Decimal


class _WireAccount(_Wire):
    as_of: datetime
    version: Optional[int] = Field(default=None, ge=0)
    cash: Decimal
    realized_pnl_today: Decimal
    orders_today: int = Field(ge=0)
    positions: List[_WirePosition] = Field(max_length=1000)
    open_orders: List[_WireOrder] = Field(max_length=1000)


class AccountFetch(NamedTuple):
    record: Optional[AccountRecord]
    issues: List[DataIssue]


class RestAccountAdapter:
    def __init__(self, source_id: str, client: ReadOnlyHttpClient, registry: InstrumentRegistry, clock: Clock,
                 account_alias: str, max_age: timedelta = timedelta(seconds=5),
                 max_future_skew: timedelta = timedelta(seconds=1), backoff: Optional[ExponentialBackoff] = None,
                 secrets=None, credential_name: Optional[str] = None) -> None:
        if credential_name and secrets is None:
            raise ValueError("credential_name requires a SecretsProvider")
        self.source_id = source_id
        self._client = client
        self._registry = registry
        self._clock = clock
        self._alias = account_alias
        self._max_age = max_age
        self._max_future_skew = max_future_skew
        self._backoff = backoff or ExponentialBackoff()
        self._secrets = secrets
        self._credential_name = credential_name
        self._next_attempt: Optional[datetime] = None
        self._last_version: Optional[int] = None
        self._log = get_logger(f"trading_agent.account_data.{source_id}")

    def __repr__(self) -> str:
        return f"RestAccountAdapter(source_id={self.source_id!r}, account_alias={self._alias!r})"

    def fetch(self) -> AccountFetch:
        now = self._clock.now()
        if self._next_attempt is not None and now < self._next_attempt:
            return AccountFetch(None, [self._issue(DataIssueCode.RATE_LIMITED,
                                                   f"backing off until {self._next_attempt.isoformat()}")])
        token = None
        if self._credential_name:
            try:
                token = self._secrets.get(self._credential_name)
            except SecretUnavailable:
                return self._fail(DataIssueCode.AUTH_UNAVAILABLE, f"secret {self._credential_name!r} unavailable")
        try:
            body = self._client.get(ACCOUNT_PATH, token)
        except RateLimited as exc:
            return self._fail(DataIssueCode.RATE_LIMITED, f"retry_after_s={exc.retry_after_s} local={exc.local}",
                              exc.retry_after_s)
        except AuthRejected:
            return self._fail(DataIssueCode.AUTH_REJECTED, "upstream rejected credentials")
        except TransportError as exc:
            return self._fail(DataIssueCode.UPSTREAM_ERROR, str(exc)[:100])
        finally:
            token = None

        received_at = self._clock.now()
        try:
            record = self._decode(body, received_at)
        except DecodeError as exc:
            return self._fail(exc.code, exc.detail)
        if record.source_ts - received_at > self._max_future_skew:
            return self._fail(DataIssueCode.FUTURE_TIMESTAMP, f"ahead by {record.source_ts - received_at}")
        if received_at - record.source_ts > self._max_age:
            return AccountFetch(None, [self._issue(DataIssueCode.ACCOUNT_STALE,
                                                   f"age {received_at - record.source_ts}")])
        if self._last_version is not None and record.version is not None and record.version < self._last_version:
            return AccountFetch(None, [self._issue(DataIssueCode.OUT_OF_ORDER,
                                                   f"version {record.version} < {self._last_version}")])
        if record.version is not None:
            self._last_version = record.version
        self._backoff.reset()
        return AccountFetch(record, [])

    # ----------------------------------------------------------------- helpers
    def _decode(self, body: bytes, received_at: datetime) -> AccountRecord:
        try:
            wire = _WireAccount.model_validate(strict_json_object(body))
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"schema invalid: {','.join(fields)[:120]}") from None
        if wire.as_of.tzinfo is None:
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "timestamp without timezone")
        try:
            positions = {}
            for p in wire.positions:
                sym = self._registry.normalize(self.source_id, p.symbol)
                if sym in positions:
                    raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "duplicate position")
                positions[sym] = PositionRecord(instrument=sym, vendor_symbol=p.symbol.strip(),
                                                quantity=p.quantity, avg_price=p.avg_price)
            orders = [OpenOrderRecord(order_ref=o.order_ref, instrument=self._registry.normalize(self.source_id, o.symbol),
                                      side=o.side, notional=o.notional, limit_price=o.limit_price)
                      for o in wire.open_orders]
            return AccountRecord(source_id=self.source_id, account_alias=self._alias, source_ts=wire.as_of,
                                 received_at=received_at, version=wire.version, cash=wire.cash,
                                 realized_pnl_today=wire.realized_pnl_today, orders_today=wire.orders_today,
                                 positions=positions, open_orders=orders)
        except UnknownInstrument:
            raise DecodeError(DataIssueCode.UNKNOWN_INSTRUMENT, "position or order symbol not in registry") from None
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"invalid values: {','.join(fields)[:120]}") from None

    def _fail(self, code: DataIssueCode, detail: str, retry_after_s: Optional[float] = None) -> AccountFetch:
        delay = self._backoff.next_delay()
        if retry_after_s is not None:
            delay = max(delay, float(retry_after_s))
        self._next_attempt = self._clock.now() + timedelta(seconds=delay)
        self._log.warning("account fetch failed source=%s code=%s", self.source_id, code.value)
        return AccountFetch(None, [self._issue(code, f"{detail}; retry in {delay:.2f}s")])

    def _issue(self, code: DataIssueCode, detail: str) -> DataIssue:
        return DataIssue(code=code, detail=detail, source_id=self.source_id)
