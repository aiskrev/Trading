from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import Field, field_validator

from trading_agent.contracts.base import Contract, require_aware
from trading_agent.core.types import Side, Symbol


class ApprovedOrder(Contract):
    """The only input execution accepts. A valid signature can only come from the RiskGateway."""

    order_id: str
    decision_id: str
    proposal_id: str
    symbol: Symbol
    side: Side
    limit_price: Decimal = Field(gt=0)
    notional: Decimal = Field(gt=0)
    issued_at: datetime
    signature: str

    _aware = field_validator("issued_at")(require_aware)

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
