"""Order idempotency (ADR 0003 verification check 6, migration step 2).

An `order_id` is recorded durably BEFORE anything is sent (RESERVED) and closed afterwards. A retry,
a replay or a restart can therefore never send the same order twice: reserving a known `order_id`
(or a known `client_order_id`) raises DuplicateOrder. The broker-side `client_order_id` is derived
from the order's identity, so even a duplicate that slipped through is recognised by the broker.

A crash between "reserved" and "closed" leaves a RESERVED record. Recovery (`resolve_unresolved`)
asks the broker whether it has the order: found => RESOLVED, with the broker's status; not found
and the order's signed lifetime is over => ABANDONED (it is never resent; a new proposal goes back
through the risk service with a new `order_id`); otherwise it stays unresolved and the caller HALTs.
A record that cannot be written means the order is not sent (fail closed).
"""
import os
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Union

from pydantic import field_validator

from trading_agent.audit.chain import ChainedLog
from trading_agent.contracts.base import Contract, require_aware
from trading_agent.contracts.execution import ApprovedOrder, BrokerAck
from trading_agent.core.errors import DuplicateOrder
from trading_agent.core.hashing import sha256_hex

CLIENT_ORDER_ID_PREFIX = "ta1-"
CLIENT_ORDER_ID_HEX = 40          # 44 characters in all: inside every broker's limit (Alpaca allows 128)


def client_order_id(order: ApprovedOrder) -> str:
    """Deterministic broker-side id: the same signed order always yields the same id, other orders never do.

    Derived from (order_id, environment, account), so a paper order and a live order never share one.
    """
    digest = sha256_hex({"order_id": order.order_id, "environment": order.environment.value,
                         "account": order.broker_account_alias})
    return CLIENT_ORDER_ID_PREFIX + digest[:CLIENT_ORDER_ID_HEX]


class Phase(str, Enum):
    RESERVED = "RESERVED"        # recorded, about to be (or maybe) sent
    COMPLETED = "COMPLETED"      # the broker answered; `outcome` is the ack status
    RESOLVED = "RESOLVED"        # recovery found it at the broker
    ABANDONED = "ABANDONED"      # recovery proved it was never sent and is expired; never resent


class IdempotencyRecord(Contract):
    order_id: str
    client_order_id: str
    phase: Phase
    at: datetime
    expires_at: datetime
    outcome: Optional[str] = None

    _aware = field_validator("at", "expires_at")(require_aware)


class Resolution(NamedTuple):
    order_id: str
    phase: Phase
    outcome: str


class IdempotencyStore:
    """Durable when opened on a path (hash-chained JSONL, torn tail moved aside); in-memory otherwise."""

    def __init__(self, log: Optional[ChainedLog] = None) -> None:
        self._log = log
        self._state: Dict[str, IdempotencyRecord] = {}
        self._by_client: Dict[str, str] = {}
        for rec in (log.items if log is not None else []):
            self._apply(rec)

    @classmethod
    def in_memory(cls) -> "IdempotencyStore":
        return cls(None)

    @classmethod
    def open(cls, path: Union[str, Path]) -> "tuple":
        log, opened = ChainedLog.open(path, IdempotencyRecord)
        return cls(log), opened

    @property
    def durable(self) -> bool:
        return self._log is not None

    # ------------------------------------------------------------------ writes
    def reserve(self, order: ApprovedOrder, at: datetime) -> str:
        """Record the order before it is sent. Raises DuplicateOrder if seen; an unwritable record raises
        AuditWriteFailure, and the caller must then not send."""
        cid = client_order_id(order)
        if order.order_id in self._state or cid in self._by_client:
            raise DuplicateOrder(order.order_id)
        self._append(IdempotencyRecord(order_id=order.order_id, client_order_id=cid, phase=Phase.RESERVED, at=at,
                                       expires_at=order.expires_at))
        return cid

    def close(self, order_id: str, phase: Phase, outcome: str, at: datetime) -> None:
        current = self._state.get(order_id)
        if current is None or current.phase is not Phase.RESERVED:
            raise KeyError(f"{order_id} is not a reserved order")
        if phase is Phase.RESERVED:
            raise ValueError("close() ends a reservation")
        self._append(current.model_copy(update={"phase": phase, "outcome": outcome, "at": at}))

    # ------------------------------------------------------------------ reads
    def get(self, order_id: str) -> Optional[IdempotencyRecord]:
        return self._state.get(order_id)

    def unresolved(self) -> List[IdempotencyRecord]:
        return [r for r in self._state.values() if r.phase is Phase.RESERVED]

    def __len__(self) -> int:
        return len(self._state)

    # ------------------------------------------------------------------ internals
    def _append(self, rec: IdempotencyRecord) -> None:
        if self._log is not None:
            self._log.append(rec)   # raises before any in-memory change, so memory never runs ahead of disk
            self._sync()
        self._apply(rec)

    def _apply(self, rec: IdempotencyRecord) -> None:
        self._state[rec.order_id] = rec
        self._by_client[rec.client_order_id] = rec.order_id

    def _sync(self) -> None:
        """fsync so a reservation survives power loss, not just a process crash."""
        with self._log.path.open("rb+") as fh:
            os.fsync(fh.fileno())


def resolve_unresolved(store: IdempotencyStore, lookup: Callable[[str], Optional[BrokerAck]], now: datetime,
                       skew: timedelta = timedelta(seconds=1)) -> "tuple":
    """Settle every RESERVED order by asking the broker. Returns (resolutions, still_unresolved).

    A lookup that raises is not caught: not knowing whether an order was sent is the caller's HALT.
    An order that is not at the broker is only abandoned once it can no longer be accepted (past
    `expires_at` plus skew); until then it stays unresolved.
    """
    resolved: List[Resolution] = []
    pending: List[IdempotencyRecord] = []
    for rec in store.unresolved():
        ack = lookup(rec.client_order_id)
        if ack is not None:
            store.close(rec.order_id, Phase.RESOLVED, ack.status.value, now)
            resolved.append(Resolution(rec.order_id, Phase.RESOLVED, ack.status.value))
        elif now > rec.expires_at + skew:
            store.close(rec.order_id, Phase.ABANDONED, "NOT_AT_BROKER", now)
            resolved.append(Resolution(rec.order_id, Phase.ABANDONED, "NOT_AT_BROKER"))
        else:
            pending.append(rec)
    return resolved, pending
