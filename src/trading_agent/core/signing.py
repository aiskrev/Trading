"""Capability signing for ApprovedOrder.

The key is generated in-process at startup and never persisted, logged or
exported, so it is not a credential. Only the risk gateway signs; only the
execution port verifies. The planner cannot import this module (import-linter).
"""
import hashlib
import hmac
import secrets
from typing import Any, Dict

from trading_agent.core.hashing import canonical_json


class OrderSigner:
    def __init__(self) -> None:
        self.__key = secrets.token_bytes(32)

    def sign(self, payload: Dict[str, Any]) -> str:
        return hmac.new(self.__key, canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: Dict[str, Any], signature: str) -> bool:
        return hmac.compare_digest(self.sign(payload), signature)

    def __repr__(self) -> str:
        return "OrderSigner(<ephemeral key>)"

    def __reduce__(self):  # refuse pickling the key out of the process
        raise TypeError("OrderSigner cannot be serialised")
