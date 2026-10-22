"""Capability signing for ApprovedOrder (ADR 0003, migration step 1).

Ed25519: the risk side holds a sign-only `Signer`; the execution side holds public keys only
(`TrustedKey`), so whoever can verify an order cannot forge one. In-process and in BACKTEST the key is
an `EphemeralSigner`, generated at startup and never persisted, logged or exported, so it is not a
credential. The vault's `VaultSigner` implements the same `Signer` port.

The planner cannot import this module (import-linter).
"""
from datetime import datetime
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_agent.core.hashing import canonical_json

ALG = "Ed25519"
PUBLIC_KEY_BYTES = 32
MAX_ORDER_TTL_SECONDS = 10    # ADR 0003: hard maximum
MAX_CLOCK_SKEW_SECONDS = 1    # ADR 0003: services are NTP-synchronised
BACKTEST_ACCOUNT_ALIAS = "backtest-sim"


def order_message(payload: Dict[str, Any]) -> bytes:
    """The exact bytes that are signed: canonical JSON of the payload (signature excluded)."""
    return canonical_json(payload).encode("utf-8")


class OrderTiming(BaseModel):
    """Lifetime of a signed order and tolerated clock skew (ADR 0003, decision 3)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ttl_seconds: float = Field(default=5, gt=0, le=MAX_ORDER_TTL_SECONDS)
    clock_skew_seconds: float = Field(default=1, ge=0, le=MAX_CLOCK_SKEW_SECONDS)


class TrustedKey(BaseModel):
    """A published verification key: the raw 32-byte Ed25519 public key and its validity window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: str = Field(min_length=1)
    public_key: bytes
    not_before: Optional[datetime] = None   # None = unbounded (ephemeral in-process keys only)
    not_after: Optional[datetime] = None
    revoked: bool = False

    @field_validator("public_key", mode="before")
    @classmethod
    def _raw_public_key_only(cls, value):
        if not isinstance(value, bytes) or len(value) != PUBLIC_KEY_BYTES:
            raise ValueError("public_key must be the 32 raw bytes of an Ed25519 public key")
        return value

    @model_validator(mode="after")
    def _window(self) -> "TrustedKey":
        for t in (self.not_before, self.not_after):
            if t is not None and t.tzinfo is None:
                raise ValueError("key validity timestamps must be timezone-aware")
        if self.not_before and self.not_after and self.not_after <= self.not_before:
            raise ValueError("not_after must be after not_before")
        return self

    def valid_at(self, t: datetime) -> bool:
        return (not self.revoked and (self.not_before is None or t >= self.not_before)
                and (self.not_after is None or t <= self.not_after))


class EphemeralSigner:
    """In-process Ed25519 `Signer`: the private key lives only in this object and has no export path."""

    def __init__(self, key_id: str = "ephemeral") -> None:
        # A fixed default id keeps audit records (which carry key_id) byte-identical across replays. The key
        # itself is fresh per instance and trusted only in-process, so equal ids across processes are harmless.
        self.__key = Ed25519PrivateKey.generate()
        self.key_id = key_id

    def sign(self, message: bytes) -> bytes:
        return self.__key.sign(message)

    def public_key(self) -> bytes:
        return self.__key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def trusted_key(self, not_before: Optional[datetime] = None,
                    not_after: Optional[datetime] = None) -> TrustedKey:
        """The public half, for an OrderVerifier."""
        return TrustedKey(key_id=self.key_id, public_key=self.public_key(), not_before=not_before,
                          not_after=not_after)

    def __repr__(self) -> str:
        return f"EphemeralSigner({self.key_id!r}, <ephemeral key>)"

    def __reduce__(self):  # refuse pickling the key out of the process
        raise TypeError("EphemeralSigner cannot be serialised")
