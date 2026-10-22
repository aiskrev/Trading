from typing import Dict, FrozenSet, NamedTuple, Optional

from pydantic import SecretStr
from typing_extensions import Protocol

from trading_agent.core.errors import AuthRejected, RateLimited, ReadOnlyViolation, TransportError
from trading_agent.market_data.rate_limit import TokenBucket


class HttpResponse(NamedTuple):
    status: int
    headers: Dict[str, str]
    body: bytes


class HttpTransport(Protocol):
    """GET-only by design: there is no method for POST/PUT/PATCH/DELETE.

    No implementation in this repo opens a real connection; a broker transport is a later,
    separately reviewed plug-in that must use a read-only-scoped credential.
    """

    def get(self, path: str, headers: Dict[str, str], timeout_s: float) -> HttpResponse: ...


class ReadOnlyHttpClient:
    """Allow-listed GETs with client-side rate limiting and status-code mapping."""

    def __init__(self, transport: HttpTransport, allowed_paths: FrozenSet[str], limiter: TokenBucket,
                 timeout_s: float = 5.0) -> None:
        self._transport = transport
        self._allowed = frozenset(allowed_paths)
        self._limiter = limiter
        self._timeout_s = timeout_s

    def get(self, path: str, token: Optional[SecretStr]) -> bytes:
        if path not in self._allowed:
            raise ReadOnlyViolation(f"path not allow-listed: {path[:64]}")
        wait = self._limiter.try_acquire()
        if wait > 0:
            raise RateLimited(wait, local=True)
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token.get_secret_value()
        try:
            resp = self._transport.get(path, headers, self._timeout_s)
        except (OSError, TimeoutError) as exc:
            raise TransportError(type(exc).__name__) from None
        finally:
            headers.clear()
        if resp.status == 200:
            return resp.body
        if resp.status == 429:
            raise RateLimited(_retry_after(resp.headers))
        if resp.status in (401, 403):
            raise AuthRejected(f"HTTP {resp.status}")
        raise TransportError(f"HTTP {resp.status}")


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    for k, v in headers.items():
        if k.lower() == "retry-after":
            try:
                return min(max(float(v), 0.0), 3600.0)
            except ValueError:
                return None  # HTTP-date form: fall back to our own backoff
    return None
