from typing import List, Optional

from pydantic import SecretStr
from typing_extensions import Protocol


class StreamTransport(Protocol):
    """Byte-level connection to a streaming vendor. Read-only by construction: the only outbound
    calls are connect/subscribe/resync. No transport in this repo opens a real network socket;
    a vendor transport is a later, separately reviewed plug-in.

    Errors: raise core.errors.TransportError (disconnect/timeout), RateLimited, AuthRejected.
    """

    def connect(self, token: Optional[SecretStr]) -> None: ...

    def subscribe(self, vendor_symbols: List[str]) -> None: ...

    def recv(self) -> Optional[bytes]:
        """Next pending message, or None if nothing is pending. Must not block for long."""

    def request_resync(self) -> bool:
        """Ask the vendor for a fresh snapshot after a gap. False if unsupported."""

    def close(self) -> None: ...
