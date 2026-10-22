"""Execution-side verification of ApprovedOrder v2 (ADR 0003, checks 1-5 and 7).

The verifier holds public keys only: it is constructed from `TrustedKey` records and refuses anything
that can sign. Every check must pass, else `OrderRefused(reason_code)`; the caller halts on it.
Check 6 (order_id idempotency) is the execution port's; check 8 (kill switch) is migration step 5.
"""
from datetime import timedelta
from typing import Callable, Dict, FrozenSet, Iterable, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from trading_agent.contracts.execution import ApprovedOrder
from trading_agent.core.clock import Clock
from trading_agent.core.errors import OrderRefused
from trading_agent.core.mode import Mode
from trading_agent.core.signing import ALG, MAX_ORDER_TTL_SECONDS, OrderTiming, TrustedKey, order_message


class OrderVerifier:
    def __init__(self, keys: Iterable[TrustedKey], environment: Mode, account_alias: str,
                 approved_policy_hashes: Iterable[str], clock: Clock,
                 leader_epoch: Union[int, Callable[[], int]] = 0, timing: OrderTiming = OrderTiming()) -> None:
        records: Dict[str, TrustedKey] = {}
        for key in keys:
            if not isinstance(key, TrustedKey):  # e.g. a Signer or a private key: verification only
                raise TypeError("OrderVerifier accepts TrustedKey (public keys) only")
            if key.key_id in records:
                raise ValueError(f"duplicate key_id {key.key_id}")
            records[key.key_id] = key
        if not records:
            raise ValueError("OrderVerifier needs at least one trusted key")
        self._keys = records
        self._environment = Mode(environment)
        self._account = account_alias
        self._policies: FrozenSet[str] = frozenset(approved_policy_hashes)
        self._clock = clock
        self._epoch = leader_epoch if callable(leader_epoch) else (lambda: leader_epoch)
        self._skew = timedelta(seconds=timing.clock_skew_seconds)

    def verify(self, order: ApprovedOrder) -> None:
        """Return only if every check passes; otherwise raise OrderRefused."""
        if not isinstance(order, ApprovedOrder):
            raise OrderRefused("NOT_AN_APPROVED_ORDER", getattr(order, "order_id", "?"))
        oid = order.order_id
        # 1. algorithm and key
        if order.alg != ALG:
            raise OrderRefused("UNKNOWN_ALG", oid)
        key = self._keys.get(order.key_id)
        if key is None:
            raise OrderRefused("UNKNOWN_KEY", oid)
        if key.revoked:
            raise OrderRefused("KEY_REVOKED", oid)
        if not key.valid_at(order.issued_at):
            raise OrderRefused("KEY_NOT_VALID_AT_ISSUE", oid)
        # 2. signature over the exact payload received
        try:
            signature = bytes.fromhex(order.signature)
            Ed25519PublicKey.from_public_bytes(key.public_key).verify(signature,
                                                                       order_message(order.signing_payload()))
        except (ValueError, InvalidSignature):
            raise OrderRefused("BAD_SIGNATURE", oid) from None
        # 3. time window (skew widens the early side only; the expiry is never extended)
        lifetime = order.expires_at - order.issued_at
        if lifetime <= timedelta(0) or lifetime > timedelta(seconds=MAX_ORDER_TTL_SECONDS):
            raise OrderRefused("BAD_LIFETIME", oid)
        now = self._clock.now()
        if now < order.issued_at - self._skew:
            raise OrderRefused("FROM_THE_FUTURE", oid)
        if now > order.expires_at:
            raise OrderRefused("EXPIRED", oid)
        # 4. bound to this service's environment and account
        if order.environment is not self._environment:
            raise OrderRefused("WRONG_ENVIRONMENT", oid)
        if order.broker_account_alias != self._account:
            raise OrderRefused("WRONG_ACCOUNT", oid)
        # 5. current leader only
        if order.leader_epoch != self._epoch():
            raise OrderRefused("STALE_EPOCH", oid)
        # 7. approved risk policy only
        if order.policy_sha256 not in self._policies:
            raise OrderRefused("UNAPPROVED_POLICY", oid)

    def __repr__(self) -> str:
        return f"OrderVerifier({sorted(self._keys)}, {self._environment.value}, {self._account!r})"
