"""Read-only streaming market-data adapter behind a transport + decoder.

poll() is non-blocking and driven by the injected clock, so reconnect/backoff timing is
deterministic in tests. Fail-closed rules:
  * any decode problem drops that message (and reconnects after too many in a row);
  * a sequence gap quarantines the affected instruments until a snapshot arrives;
  * a disconnect, stale heartbeat or rate limit clears the book (held quotes are untrusted);
  * credentials are fetched per connect from SecretsProvider and never logged or stored.
"""
from datetime import datetime, timedelta
from enum import Enum
from typing import List, NamedTuple, Optional

from pydantic import Field
from typing_extensions import Literal

from trading_agent.contracts.base import Contract
from trading_agent.contracts.data_quality import DataIssue, DataIssueCode
from trading_agent.core.clock import Clock
from trading_agent.core.errors import AuthRejected, RateLimited, SecretUnavailable, TransportError
from trading_agent.market_data.backoff import ExponentialBackoff
from trading_agent.market_data.decoders import GenericJsonQuoteDecoder
from trading_agent.market_data.ports import RawTick
from trading_agent.market_data.quote_book import QuoteBook
from trading_agent.market_data.records import QuoteRecord
from trading_agent.market_data.sequence import SequenceTracker, SeqStatus
from trading_agent.market_data.transport import StreamTransport
from trading_agent.observability.logging import get_logger


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTED = "CONNECTED"
    BACKOFF = "BACKOFF"


class FeedHealth(Contract):
    source_id: str
    state: ConnectionState
    connects: int = Field(ge=0)
    reconnects: int = Field(ge=0)
    last_message_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None
    messages: int = Field(ge=0)
    malformed: int = Field(ge=0)
    gaps: int = Field(ge=0)
    late: int = Field(ge=0)
    rate_limited: int = Field(ge=0)


class FeedBatch(NamedTuple):
    records: List[QuoteRecord]
    issues: List[DataIssue]
    health: FeedHealth


class StreamingQuoteAdapter:
    def __init__(self, source_id: str, transport: StreamTransport, decoder: GenericJsonQuoteDecoder,
                 vendor_symbols: List[str], clock: Clock, book: QuoteBook,
                 backoff: Optional[ExponentialBackoff] = None, secrets=None, credential_name: Optional[str] = None,
                 heartbeat_timeout: timedelta = timedelta(seconds=5),
                 sequence_scope: Literal["feed", "instrument"] = "feed",
                 max_messages_per_poll: int = 1000, max_consecutive_malformed: int = 20) -> None:
        if credential_name and secrets is None:
            raise ValueError("credential_name requires a SecretsProvider")
        self.source_id = source_id
        self._transport = transport
        self._decoder = decoder
        self._symbols = list(vendor_symbols)
        self._clock = clock
        self.book = book
        self._backoff = backoff or ExponentialBackoff()
        self._secrets = secrets
        self._credential_name = credential_name
        self._heartbeat_timeout = heartbeat_timeout
        self._scope = sequence_scope
        self._max_per_poll = max_messages_per_poll
        self._max_malformed = max_consecutive_malformed
        self._seq = SequenceTracker()
        self._log = get_logger(f"trading_agent.market_data.{source_id}")

        self.state = ConnectionState.DISCONNECTED
        self._next_attempt: Optional[datetime] = None
        self._last_message_at: Optional[datetime] = None
        self._consecutive_malformed = 0
        self._counts = {"connects": 0, "reconnects": 0, "messages": 0, "malformed": 0, "gaps": 0, "late": 0,
                        "rate_limited": 0}

    def __repr__(self) -> str:
        return f"StreamingQuoteAdapter(source_id={self.source_id!r}, state={self.state.value})"

    # ------------------------------------------------------------------ public
    def poll(self) -> FeedBatch:
        issues: List[DataIssue] = []
        records: List[QuoteRecord] = []
        if self.state is not ConnectionState.CONNECTED and self._due():
            self._connect(issues)
        if self.state is ConnectionState.CONNECTED:
            for _ in range(self._max_per_poll):
                try:
                    raw = self._transport.recv()
                except RateLimited as exc:
                    self._rate_limited(exc, issues)
                    break
                except AuthRejected:
                    self._drop(DataIssueCode.AUTH_REJECTED, "upstream rejected credentials", issues)
                    break
                except TransportError as exc:
                    self._drop(DataIssueCode.DISCONNECTED, type(exc).__name__, issues)
                    break
                if raw is None:
                    break
                self._handle(raw, records, issues)
                if self.state is not ConnectionState.CONNECTED:
                    break
        if self.state is ConnectionState.CONNECTED and self._last_message_at is not None:
            silent = self._clock.now() - self._last_message_at
            if silent > self._heartbeat_timeout:
                self._drop(DataIssueCode.STALE_FEED, f"no message for {silent}", issues)
        issues.extend(self.book.stale_issues(self._clock.now()))
        return FeedBatch(records, issues, self.health())

    def fresh_ticks(self) -> List[RawTick]:
        """Quotes safe to put into a MarketSnapshot right now (fresh, not quarantined)."""
        return [r.to_raw_tick() for r in self.book.fresh(self._clock.now())]

    def health(self) -> FeedHealth:
        return FeedHealth(source_id=self.source_id, state=self.state, last_message_at=self._last_message_at,
                          next_attempt_at=self._next_attempt, **self._counts)

    # ----------------------------------------------------------------- helpers
    def _due(self) -> bool:
        return self._next_attempt is None or self._clock.now() >= self._next_attempt

    def _connect(self, issues: List[DataIssue]) -> None:
        token = None
        if self._credential_name:
            try:
                token = self._secrets.get(self._credential_name)
            except SecretUnavailable:
                self._schedule(DataIssueCode.AUTH_UNAVAILABLE, f"secret {self._credential_name!r} unavailable", issues)
                return
        try:
            self._transport.connect(token)
            self._transport.subscribe(self._symbols)
        except RateLimited as exc:
            self._counts["rate_limited"] += 1
            self._close_quietly()
            self._schedule(DataIssueCode.RATE_LIMITED, f"on connect, retry_after_s={exc.retry_after_s}", issues,
                           exc.retry_after_s)
            return
        except AuthRejected:
            self._close_quietly()
            self._schedule(DataIssueCode.AUTH_REJECTED, "upstream rejected credentials", issues)
            return
        except TransportError as exc:
            self._close_quietly()
            self._schedule(DataIssueCode.DISCONNECTED, f"connect failed: {type(exc).__name__}", issues)
            return
        finally:
            token = None  # drop our reference immediately
        self._counts["connects"] += 1
        if self._counts["connects"] > 1:
            self._counts["reconnects"] += 1
        self.state = ConnectionState.CONNECTED
        self._next_attempt = None
        self._last_message_at = self._clock.now()  # grace period starts at connect
        self._seq.reset()
        self._log.info("connected source=%s attempt=%d", self.source_id, self._backoff.attempts)

    def _handle(self, raw: bytes, records: List[QuoteRecord], issues: List[DataIssue]) -> None:
        received_at = self._clock.now()
        self._counts["messages"] += 1
        try:
            msg = self._decoder.decode(raw, received_at)
        except RateLimited as exc:
            self._rate_limited(exc, issues)
            return
        except AuthRejected:
            self._drop(DataIssueCode.AUTH_REJECTED, "upstream rejected credentials", issues)
            return
        except Exception as exc:  # DecodeError or anything unexpected: drop the message
            code = getattr(exc, "code", DataIssueCode.MALFORMED_MESSAGE)
            detail = getattr(exc, "detail", type(exc).__name__)
            self._counts["malformed"] += 1
            self._consecutive_malformed += 1
            issues.append(DataIssue(code=code, detail=detail, source_id=self.source_id))
            if self._consecutive_malformed >= self._max_malformed:
                self._drop(DataIssueCode.MALFORMED_MESSAGE, "too many consecutive malformed messages", issues)
            return
        self._consecutive_malformed = 0
        self._last_message_at = received_at

        if msg.kind == "error":
            self._drop(DataIssueCode.UPSTREAM_ERROR, f"vendor error {msg.error_code}", issues)
            return
        if msg.seq is not None and not self._check_sequence(msg, issues):
            return
        if msg.record is not None:
            problem = self.book.apply(msg.record)
            if problem is not None:
                if problem.code is DataIssueCode.LATE_MESSAGE:
                    self._counts["late"] += 1
                issues.append(problem)
                return
            records.append(msg.record)
        self._backoff.reset()  # a valid message proves the connection is healthy

    def _check_sequence(self, msg, issues: List[DataIssue]) -> bool:
        if self._scope == "instrument":
            if msg.record is None:
                return True  # heartbeats carry no per-instrument sequence
            key = msg.record.instrument
        else:
            key = "*"
        check = self._seq.observe(key, msg.seq)
        if check.status is SeqStatus.GAP:
            self._counts["gaps"] += 1
            issues.append(DataIssue(code=DataIssueCode.SEQUENCE_GAP, symbol=None if key == "*" else key,
                                    detail=f"expected {check.expected} got {check.got}", source_id=self.source_id))
            self.book.quarantine(None if key == "*" else [key])
            self._transport.request_resync()
            return True  # still process: if this message is a snapshot it releases the quarantine
        if check.status in (SeqStatus.DUPLICATE, SeqStatus.OUT_OF_ORDER):
            code = DataIssueCode.DUPLICATE_MESSAGE if check.status is SeqStatus.DUPLICATE else DataIssueCode.OUT_OF_ORDER
            issues.append(DataIssue(code=code, symbol=None if key == "*" else key,
                                    detail=f"seq {check.got} after {check.expected - 1}", source_id=self.source_id))
            return False
        return True

    def _rate_limited(self, exc: RateLimited, issues: List[DataIssue]) -> None:
        self._counts["rate_limited"] += 1
        self._drop(DataIssueCode.RATE_LIMITED, f"retry_after_s={exc.retry_after_s}", issues, exc.retry_after_s)

    def _drop(self, code: DataIssueCode, detail: str, issues: List[DataIssue],
              retry_after_s: Optional[float] = None) -> None:
        self._close_quietly()
        self.book.clear()
        self._seq.reset()
        self._schedule(code, detail, issues, retry_after_s)
        self._log.warning("feed down source=%s code=%s", self.source_id, code.value)

    def _schedule(self, code: DataIssueCode, detail: str, issues: List[DataIssue],
                  retry_after_s: Optional[float] = None) -> None:
        delay = self._backoff.next_delay()
        if retry_after_s is not None:
            delay = max(delay, float(retry_after_s))
        self.state = ConnectionState.BACKOFF
        self._next_attempt = self._clock.now() + timedelta(seconds=delay)
        issues.append(DataIssue(code=code, detail=f"{detail}; retry in {delay:.2f}s", source_id=self.source_id))

    def _close_quietly(self) -> None:
        try:
            self._transport.close()
        except Exception:
            pass
