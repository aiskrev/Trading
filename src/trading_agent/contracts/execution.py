from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import Field, field_validator
from typing_extensions import Literal

from trading_agent.contracts.base import Contract, require_aware
from trading_agent.core.mode import Mode
from trading_agent.core.types import Side, Symbol


class ApprovedOrder(Contract):
    """The only input execution accepts (v2, ADR 0003). A valid signature can only come from the risk side.

    The Ed25519 signature covers every field except `signature`, so the order is bound to one
    environment, one broker account, one leader epoch, one approved policy and a short time window.
    """

    order_version: Literal[2] = 2
    order_id: str = Field(min_length=1)
    decision_id: str
    proposal_id: str
    symbol: Symbol
    side: Side
    limit_price: Decimal = Field(gt=0)
    notional: Decimal = Field(gt=0)
    issued_at: datetime
    expires_at: datetime
    environment: Mode
    broker_account_alias: str = Field(min_length=1)
    leader_epoch: int = Field(ge=0)
    policy_version: str
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    market_snapshot_id: str
    account_snapshot_id: str
    key_id: str = Field(min_length=1)
    alg: Literal["Ed25519"] = "Ed25519"
    signature: str   # hex; never written to the audit log or a DecisionPacket

    _aware = field_validator("issued_at", "expires_at")(require_aware)

    def signing_payload(self) -> Dict[str, Any]:
        return self.model_dump(mode="json", exclude={"signature"})


class AckStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class BrokerAck(Contract):
    order_id: str
    status: AckStatus
    reason: Optional[str] = None
    at: datetime


class Fill(Contract):
    fill_id: str
    order_id: str
    symbol: Symbol
    side: Side
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    fee: Decimal = Field(ge=0)
    at: datetime

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price
