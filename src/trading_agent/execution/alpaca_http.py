"""HTTP access to the Alpaca PAPER trading API, for the execution service only (roadmap P6).

Exactly these requests can be made; anything else is refused before a connection is opened:

  POST   /v2/orders                                        place one order (body built from an ApprovedOrder)
  GET    /v2/orders/{uuid}                                 one order's state
  DELETE /v2/orders/{uuid}                                 cancel one order (never "cancel all")
  GET    /v2/orders:by_client_order_id?client_order_id=ta1-<40 hex>    recovery lookup
  GET    /v2/positions                                     reconciliation (never "close all")
  GET    /v2/account                                       cash at session start

The host is the paper host, a constant: there is no parameter for it and the live host appears nowhere
in this package. Credential: one vault secret "KEY_ID:SECRET", read per request, sent only as headers.
"""
import http.client
import json
import re
import ssl
from decimal import Decimal
from typing import Any, Dict, NamedTuple, Optional, Tuple

from typing_extensions import Protocol

from trading_agent.core.clock import Clock
from trading_agent.core.errors import AuthRejected, RateLimited, TransportError
from trading_agent.market_data.alpaca_stream import split_credential
from trading_agent.market_data.rate_limit import TokenBucket
from trading_agent.market_data.wire import _no_duplicate_keys, _reject_constant

PAPER_HOST = "paper-api.alpaca.markets"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ALLOWED = (
    ("POST", re.compile(r"^/v2/orders$")),
    ("GET", re.compile(rf"^/v2/orders/{_UUID}$")),
    ("DELETE", re.compile(rf"^/v2/orders/{_UUID}$")),
    ("GET", re.compile(r"^/v2/orders:by_client_order_id\?client_order_id=ta1-[0-9a-f]{40}$")),
    ("GET", re.compile(r"^/v2/positions$")),
    ("GET", re.compile(r"^/v2/account$")),
)


class RawResponse(NamedTuple):
    status: int
    body: bytes


class TradingTransport(Protocol):
    def request(self, method: str, path: str, headers: Dict[str, str], body: Optional[bytes],
                timeout_s: float) -> RawResponse: ...


class HttpsTradingTransport:
    """TLS to the paper host only (system trust store, hostname check, TLS >= 1.2)."""

    def __init__(self) -> None:
        self._ctx = ssl.create_default_context()
        self._ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def request(self, method: str, path: str, headers: Dict[str, str], body: Optional[bytes],
                timeout_s: float) -> RawResponse:
        conn = http.client.HTTPSConnection(PAPER_HOST, 443, timeout=timeout_s, context=self._ctx)
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise OSError("response too large")
            return RawResponse(resp.status, data)
        except http.client.HTTPException as exc:
            raise OSError(type(exc).__name__) from None
        finally:
            conn.close()


class AlpacaTradingClient:
    def __init__(self, transport: TradingTransport, secrets, credential_name: str, clock: Clock,
                 limiter: Optional[TokenBucket] = None, timeout_s: float = 5.0) -> None:
        self._transport, self._secrets, self._name = transport, secrets, credential_name
        self._limiter = limiter or TokenBucket(rate_per_s=2.0, capacity=10, clock=clock)
        self._timeout = timeout_s

    def request(self, method: str, path: str, payload: Optional[Dict[str, str]] = None) -> Tuple[int, Any]:
        """(status, parsed JSON or None). Raises TransportError (no answer: state unknown), RateLimited
        (local limit, nothing sent), AuthRejected (401). Other statuses are returned for the caller to map."""
        if not any(m == method and p.match(path) for m, p in ALLOWED):
            raise TransportError("request not allow-listed")
        if (payload is not None) != (method == "POST"):
            raise TransportError("only POST carries a body")
        wait = self._limiter.try_acquire()
        if wait > 0:
            raise RateLimited(wait, local=True)
        key, secret = split_credential(self._secrets.get(self._name))
        headers = {"Accept": "application/json", "APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        key = secret = None  # noqa: F841
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            resp = self._transport.request(method, path, headers, body, self._timeout)
        except (OSError, ValueError) as exc:
            raise TransportError(type(exc).__name__) from None
        finally:
            headers.clear()
        if resp.status == 401:
            raise AuthRejected("HTTP 401")
        return resp.status, _loads(resp.body)


def _loads(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"), parse_float=Decimal, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except Exception:
        return None   # unreadable body: the caller treats the answer by status and shape
