"""Alpaca real-time stock data (roadmap P3): a StreamTransport and a decoder for StreamingQuoteAdapter.

Wire format (docs.alpaca.markets, "Real-time Stock Pricing Data" and "Streaming Market Data"):
  * wss://stream.data.alpaca.markets/v2/{iex|sip|delayed_sip|test}; every server frame is a JSON ARRAY of
    objects with a type field "T"; control messages: success (connected/authenticated), subscription, error;
  * auth: {"action":"auth","key":...,"secret":...} within 10 s of connecting; one connection per account;
  * quotes: {"T":"q","S":sym,"bp","bs","ap","as","bx","ax","c","t","z"}; trades: {"T":"t","S","p","s","t",...};
  * errors: {"T":"error","code":402,"msg":"auth failed"} (400..410, 500).

Read-only by construction: the only messages sent are auth, subscribe and WebSocket pings. The credential is
one vault secret "KEY_ID:SECRET" (read-only paper keys), fetched per connect by the adapter and dropped
after the auth message is sent. Hosts are an allow-list; there is no way to point the transport elsewhere.

Fail-closed choices, documented:
  * a one-sided quote (bid or ask 0, common on IEX) updates nothing: the previous quote ages out of the book;
  * `last` is the latest trade price seen on this connection, else the quote mid (Alpaca quotes carry no last);
  * Alpaca sends no heartbeat; a WebSocket ping every `ping_every_s` and its pong prove liveness and are
    passed on as a synthetic {"T":"_heartbeat"} message (Alpaca never sends that type);
  * sizes are reported in round lots and multiplied by `lot_size` (default 100) to shares.
"""
import json
import time
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.errors import AuthRejected, RateLimited, TransportError, UnknownInstrument
from trading_agent.market_data.decoders import Decoded
from trading_agent.market_data.instruments import InstrumentRegistry
from trading_agent.market_data.records import QuoteRecord
from trading_agent.market_data.websocket import SocketFactory, WebSocketClient, tls_socket
from trading_agent.market_data.wire import DEFAULT_MAX_BYTES, DecodeError, _no_duplicate_keys, strict_json_object

ALLOWED_HOSTS = frozenset({"stream.data.alpaca.markets", "stream.data.sandbox.alpaca.markets"})
FEEDS = {"iex": "/v2/iex", "sip": "/v2/sip", "delayed_sip": "/v2/delayed_sip", "test": "/v2/test"}
HEARTBEAT = b'{"T":"_heartbeat"}'
MAX_SYMBOLS = 30          # the free plan's symbol limit; the universe is far smaller
AUTH_REJECTED_CODES = {402, 409}            # auth failed, insufficient subscription
RATE_LIMIT_CODES = {406: 60.0, 407: 10.0}   # connection limit, slow client -> retry after (s)


def split_credential(token: Optional[SecretStr]):
    """'KEY_ID:SECRET' -> (key_id, secret). Never echoes the value."""
    value = token.get_secret_value() if isinstance(token, SecretStr) else ""
    key, sep, secret = value.partition(":")
    if not sep or not key or not secret:
        raise AuthRejected("credential must be one secret of the form KEY_ID:SECRET")
    return key, secret


class AlpacaStreamTransport:
    def __init__(self, feed: str = "iex", host: str = "stream.data.alpaca.markets", ping_every_s: float = 15.0,
                 socket_factory: SocketFactory = tls_socket, monotonic: Callable[[], float] = time.monotonic,
                 answer_timeout_s: float = 10.0) -> None:
        if host not in ALLOWED_HOSTS:
            raise ValueError(f"host not allowed for the Alpaca stream: {host!r}")
        if feed not in FEEDS:
            raise ValueError(f"unknown Alpaca feed {feed!r}")
        self._ws = WebSocketClient(host, FEEDS[feed], socket_factory=socket_factory, timeout_s=answer_timeout_s,
                                   extra_headers={"Content-Type": "application/json"})
        self._ping_every, self._now, self._answer_timeout = ping_every_s, monotonic, answer_timeout_s
        self._queue: List[bytes] = []
        self._last_ping = 0.0
        self._pongs_seen = 0

    # ------------------------------------------------------------------ StreamTransport
    def connect(self, token: Optional[SecretStr]) -> None:
        key, secret = split_credential(token)
        self._queue = []
        self._ws.connect()
        try:
            self._expect("success", "connected")
            self._ws.send_text(json.dumps({"action": "auth", "key": key, "secret": secret}))
        finally:
            key = secret = None  # noqa: F841 (drop our references)
        self._expect("success", "authenticated")
        self._last_ping, self._pongs_seen = self._now(), self._ws.pongs

    def subscribe(self, vendor_symbols: List[str]) -> None:
        symbols = sorted(set(vendor_symbols))
        if not symbols or len(symbols) > MAX_SYMBOLS:
            raise TransportError("subscribe: 1..30 symbols")
        self._ws.send_text(json.dumps({"action": "subscribe", "quotes": symbols, "trades": symbols}))
        deadline = self._now() + self._answer_timeout
        while self._now() < deadline:
            for item in self._items(self._ws.recv_blocking(max(deadline - self._now(), 0.01))):
                if item.get("T") == "subscription":
                    if not set(symbols) <= set(item.get("quotes") or []):
                        raise TransportError("subscription not confirmed for every symbol")
                    return
                self._control(item)
                self._queue.append(_encode(item))
        raise TransportError("subscription not confirmed in time")

    def recv(self) -> Optional[bytes]:
        if not self._queue:
            if self._now() - self._last_ping >= self._ping_every:
                self._ws.ping()
                self._last_ping = self._now()
            for text in self._ws.poll(0.0):
                try:
                    items = self._items(text)
                except DecodeError:
                    self._queue.append(text.encode("utf-8")[:DEFAULT_MAX_BYTES + 1])  # decoder counts it
                    continue
                self._queue.extend(_encode(i) for i in items)
            if self._ws.pongs != self._pongs_seen:
                self._pongs_seen = self._ws.pongs
                self._queue.append(HEARTBEAT)
        return self._queue.pop(0) if self._queue else None

    def request_resync(self) -> bool:
        return False  # Alpaca has no sequence numbers and no resync request

    def close(self) -> None:
        self._queue = []
        self._ws.close()

    # ------------------------------------------------------------------ helpers
    def _expect(self, kind: str, msg: str) -> None:
        deadline = self._now() + self._answer_timeout
        while self._now() < deadline:
            for item in self._items(self._ws.recv_blocking(max(deadline - self._now(), 0.01))):
                self._control(item)
                if item.get("T") == kind and item.get("msg") == msg:
                    return
        raise TransportError(f"no '{msg}' from the Alpaca stream")

    @staticmethod
    def _control(item: Dict) -> None:
        if item.get("T") != "error":
            return
        code = item.get("code")
        if code in AUTH_REJECTED_CODES:
            raise AuthRejected(f"alpaca stream error {code}")
        if code in RATE_LIMIT_CODES:
            raise RateLimited(RATE_LIMIT_CODES[code])
        raise TransportError(f"alpaca stream error {code if isinstance(code, int) else '?'}")

    @staticmethod
    def _items(text: str) -> List[Dict]:
        raw = text.encode("utf-8")
        if len(raw) > 4 * 1024 * 1024:
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "frame too large")
        try:
            data = json.loads(text, parse_float=Decimal, parse_constant=_no_constant,
                              object_pairs_hook=_no_duplicate_keys)
        except (ValueError, DecodeError):
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "frame is not JSON") from None
        if not isinstance(data, list) or not all(isinstance(i, dict) for i in data):
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "frame is not an array of objects")
        return data


def _no_constant(_):
    raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "non-finite number")


def _encode(item: Dict) -> bytes:
    """Re-encode one message; Decimals become strings so no precision is lost."""
    return json.dumps(item, default=lambda o: str(o) if isinstance(o, Decimal) else None,
                      separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------------- decoder
class _Msg(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class _Quote(_Msg):
    S: str = Field(max_length=32)
    bp: Decimal = Field(ge=0)
    ap: Decimal = Field(ge=0)
    bs: Decimal = Field(ge=0)
    as_: Decimal = Field(ge=0, alias="as")
    t: datetime


class _Trade(_Msg):
    S: str = Field(max_length=32)
    p: Decimal = Field(gt=0)
    t: datetime


IGNORED_TYPES = {"success", "subscription", "_heartbeat", "b", "d", "u", "s", "c", "x", "l", "i"}


class AlpacaQuoteDecoder:
    """Same interface as GenericJsonQuoteDecoder: decode(raw, received_at) -> Decoded (one message)."""

    def __init__(self, source_id: str, registry: InstrumentRegistry, lot_size: int = 100,
                 max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.source_id, self.registry, self.max_bytes = source_id, registry, max_bytes
        self._lot = Decimal(lot_size)
        self._last_trade: Dict[str, Decimal] = {}

    def decode(self, raw: bytes, received_at: datetime) -> Decoded:
        data = strict_json_object(raw, self.max_bytes)
        kind = data.get("T")
        if kind == "q":
            return self._quote(data, received_at)
        if kind == "t":
            trade = self._validate(_Trade, data)
            try:
                self._last_trade[self.registry.normalize(self.source_id, trade.S)] = trade.p
            except UnknownInstrument:
                raise DecodeError(DataIssueCode.UNKNOWN_INSTRUMENT, "symbol not in instrument registry") from None
            return Decoded("heartbeat", None)
        if kind == "error":
            code = data.get("code")
            if code in AUTH_REJECTED_CODES:
                raise AuthRejected(f"alpaca stream error {code}")
            if code in RATE_LIMIT_CODES:
                raise RateLimited(RATE_LIMIT_CODES[code])
            return Decoded("error", None, error_code=f"ALPACA_{code if isinstance(code, int) else 'UNKNOWN'}")
        if kind in IGNORED_TYPES:
            return Decoded("heartbeat", None)
        raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "unknown or missing message type")

    def _quote(self, data, received_at: datetime) -> Decoded:
        q = self._validate(_Quote, data)
        if q.t.tzinfo is None:
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, "timestamp without timezone")
        try:
            instrument = self.registry.normalize(self.source_id, q.S)
        except UnknownInstrument:
            raise DecodeError(DataIssueCode.UNKNOWN_INSTRUMENT, "symbol not in instrument registry") from None
        if q.bp <= 0 or q.ap <= 0:
            return Decoded("heartbeat", None)    # one-sided quote: nothing to update
        last = self._last_trade.get(instrument) or (q.bp + q.ap) / 2
        try:
            record = QuoteRecord(source_id=self.source_id, instrument=instrument, vendor_symbol=q.S.strip(),
                                 seq=None, source_ts=q.t, received_at=received_at, bid=q.bp, ask=q.ap,
                                 bid_size=q.bs * self._lot, ask_size=q.as_ * self._lot, last=last)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"invalid values: {','.join(fields)[:120]}") from None
        return Decoded("quote", None, record)

    @staticmethod
    def _validate(model, data):
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise DecodeError(DataIssueCode.MALFORMED_MESSAGE, f"schema invalid: {','.join(fields)[:120]}") from None
