"""In-memory stream transport for BACKTEST/tests. Opens no sockets."""
from collections import deque
from typing import Deque, List, Optional, Sequence, Union

from pydantic import SecretStr

from trading_agent.core.errors import TransportError

Event = Union[bytes, Exception, None]  # None = "nothing pending this recv"


class ScriptedStreamTransport:
    def __init__(self, sessions: Sequence[Sequence[Event]] = (), connect_errors: Sequence[Optional[Exception]] = (),
                 supports_resync: bool = True) -> None:
        self._sessions: Deque[List[Event]] = deque(list(s) for s in sessions)
        self._connect_errors: Deque[Optional[Exception]] = deque(connect_errors)
        self._queue: Deque[Event] = deque()
        self.supports_resync = supports_resync
        self.connected = False
        self.connect_calls = 0
        self.subscriptions: List[List[str]] = []
        self.resync_requests = 0
        self.closed = 0
        self.token_was_supplied: List[bool] = []
        self._last_token: Optional[SecretStr] = None

    def connect(self, token: Optional[SecretStr]) -> None:
        self.connect_calls += 1
        self.token_was_supplied.append(token is not None)
        self._last_token = token
        if self._connect_errors:
            err = self._connect_errors.popleft()
            if err is not None:
                raise err
        self._queue = deque(self._sessions.popleft() if self._sessions else [])
        self.connected = True

    def subscribe(self, vendor_symbols: List[str]) -> None:
        if not self.connected:
            raise TransportError("not connected")
        self.subscriptions.append(list(vendor_symbols))

    def recv(self) -> Optional[bytes]:
        if not self.connected:
            raise TransportError("not connected")
        if not self._queue:
            return None
        ev = self._queue.popleft()
        if isinstance(ev, Exception):
            if isinstance(ev, TransportError):
                self.connected = False
            raise ev
        return ev

    def push(self, *events: Event) -> None:
        """Deliver more events on the current connection."""
        self._queue.extend(events)

    def request_resync(self) -> bool:
        self.resync_requests += 1
        return self.supports_resync

    def close(self) -> None:
        self.closed += 1
        self.connected = False

    def last_token_matches(self, value: str) -> bool:
        return self._last_token is not None and self._last_token.get_secret_value() == value
