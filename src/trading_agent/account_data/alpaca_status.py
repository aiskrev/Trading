"""Alpaca market and instrument status (read-only), for the evaluation gate and the risk gateway.

Endpoints (Alpaca Trading API v2, paper host only): GET /v2/clock and GET /v2/assets/{symbol}. Auth headers
APCA-API-KEY-ID / APCA-API-SECRET-KEY from one vault secret "KEY_ID:SECRET", read per request and dropped.

Mapping (anything else, or any error, is UNKNOWN => NO_TRADE):
  * venue: clock.is_open and now < next_close => OPEN; not is_open => CLOSED. The clock answer is cached for
    `clock_ttl_s`; if Alpaca's timestamp differs from our clock by more than `max_skew_s`, UNKNOWN (CLOCK_SKEW);
  * instrument: asset.status == "active" and asset.tradable => TRADING, otherwise HALTED. Known limit: an
    exchange halt is not visible here (it needs the SIP status channel); a halted symbol stops quoting, so the
    quote-freshness rules deny it.

The live trading host (api.alpaca.markets) is deliberately not in the allow-list.
"""
import re
from datetime import datetime, timedelta
from typing import Callable, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, ValidationError

from trading_agent.account_data.http import HttpTransport
from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.core.clock import Clock
from trading_agent.core.errors import AuthRejected, RateLimited, SecretUnavailable, TransportError
from trading_agent.market_data.alpaca_stream import split_credential
from trading_agent.market_data.rate_limit import TokenBucket
from trading_agent.market_data.wire import DecodeError, strict_json_object
from trading_agent.observability.logging import get_logger

PAPER_TRADING_HOST = "paper-api.alpaca.markets"
ALLOWED_HOSTS = frozenset({PAPER_TRADING_HOST})
_PATHS = (re.compile(r"^/v2/clock$"), re.compile(r"^/v2/assets/[A-Z][A-Z0-9.]{0,14}$"))


class _Clock(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class _Asset(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    symbol: str
    status: str
    tradable: bool


class AlpacaReadOnlyClient:
    """Allow-listed GETs with Alpaca's auth headers, a client-side rate limit and status mapping."""

    def __init__(self, transport: HttpTransport, secrets, credential_name: str, clock: Clock,
                 limiter: Optional[TokenBucket] = None, timeout_s: float = 5.0) -> None:
        self._transport, self._secrets, self._name = transport, secrets, credential_name
        self._limiter = limiter or TokenBucket(rate_per_s=2.0, capacity=10, clock=clock)   # Alpaca: 200/min
        self._timeout = timeout_s

    def get_json(self, path: str) -> Dict:
        if not any(p.match(path) for p in _PATHS):
            raise TransportError("path not allow-listed")
        wait = self._limiter.try_acquire()
        if wait > 0:
            raise RateLimited(wait, local=True)
        key, secret = split_credential(self._secrets.get(self._name))
        headers = {"Accept": "application/json", "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        key = secret = None  # noqa: F841
        try:
            resp = self._transport.get(path, headers, self._timeout)
        except (OSError, ValueError) as exc:
            raise TransportError(type(exc).__name__) from None
        finally:
            headers.clear()
        if resp.status == 429:
            raise RateLimited(None)
        if resp.status in (401, 403):
            raise AuthRejected(f"HTTP {resp.status}")
        if resp.status != 200:
            raise TransportError(f"HTTP {resp.status}")
        try:
            return strict_json_object(resp.body, 256 * 1024)
        except DecodeError as exc:
            raise TransportError(f"bad body: {exc.code.value}") from None


class AlpacaMarketStatus:
    """Callables for LiveLoopRunner: venue_status(t) and instrument_status(symbol). Never raise."""

    def __init__(self, client: AlpacaReadOnlyClient, clock: Clock, clock_ttl_s: float = 30.0,
                 asset_ttl_s: float = 300.0, max_skew_s: float = 2.0) -> None:
        self._client, self._clock = client, clock
        self._clock_ttl, self._asset_ttl = timedelta(seconds=clock_ttl_s), timedelta(seconds=asset_ttl_s)
        self._max_skew = timedelta(seconds=max_skew_s)
        self._venue: Optional[Tuple[datetime, Optional[_Clock]]] = None
        self._assets: Dict[str, Tuple[datetime, InstrumentStatus]] = {}
        self.last_problem: Optional[str] = None
        self._log = get_logger("trading_agent.account_data.alpaca_status")

    def venue_status(self, t: datetime) -> VenueStatus:
        now = self._clock.now()
        if self._venue is None or now - self._venue[0] >= self._clock_ttl:
            self._venue = (now, self._fetch_clock(now))
        answer = self._venue[1]
        if answer is None:
            return VenueStatus.UNKNOWN
        if not answer.is_open:
            return VenueStatus.CLOSED
        return VenueStatus.OPEN if t < answer.next_close else VenueStatus.UNKNOWN

    def instrument_status(self, symbol: str) -> InstrumentStatus:
        now = self._clock.now()
        cached = self._assets.get(symbol)
        if cached is None or now - cached[0] >= self._asset_ttl:
            cached = (now, self._fetch_asset(symbol))
            if cached[1] is InstrumentStatus.UNKNOWN:
                self._assets.pop(symbol, None)   # do not cache a failure: ask again next time
                return InstrumentStatus.UNKNOWN
            self._assets[symbol] = cached
        return cached[1]

    # ------------------------------------------------------------------ helpers
    def _fetch_clock(self, now: datetime) -> Optional[_Clock]:
        try:
            answer = _Clock.model_validate(self._client.get_json("/v2/clock"))
        except (TransportError, RateLimited, AuthRejected, SecretUnavailable, ValidationError) as exc:
            return self._fail(f"clock: {type(exc).__name__}")
        if any(t.tzinfo is None for t in (answer.timestamp, answer.next_open, answer.next_close)):
            return self._fail("clock: naive timestamp")
        if abs(answer.timestamp - now) > self._max_skew:
            return self._fail("CLOCK_SKEW")
        return answer

    def _fetch_asset(self, symbol: str) -> InstrumentStatus:
        if not re.match(r"^[A-Z][A-Z0-9.]{0,14}$", symbol or ""):
            return InstrumentStatus.UNKNOWN
        try:
            asset = _Asset.model_validate(self._client.get_json(f"/v2/assets/{symbol}"))
        except (TransportError, RateLimited, AuthRejected, SecretUnavailable, ValidationError) as exc:
            self._fail(f"asset {symbol}: {type(exc).__name__}")
            return InstrumentStatus.UNKNOWN
        if asset.symbol != symbol:
            self._fail(f"asset {symbol}: answer for another symbol")
            return InstrumentStatus.UNKNOWN
        return InstrumentStatus.TRADING if asset.status == "active" and asset.tradable else InstrumentStatus.HALTED

    def _fail(self, problem: str) -> None:
        self.last_problem = problem
        self._log.warning("alpaca status unavailable: %s", problem)
        return None


def market_status_callables(status: AlpacaMarketStatus) -> Tuple[Callable[[str], InstrumentStatus],
                                                                   Callable[[datetime], VenueStatus]]:
    return status.instrument_status, status.venue_status
