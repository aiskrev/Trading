from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import Field, model_validator
from typing_extensions import Annotated

from trading_agent.contracts.base import Contract
from trading_agent.core.types import EvidenceRef, Side, Symbol

ShortText = Annotated[str, Field(min_length=1, max_length=300)]


class Decision(str, Enum):
    TRADE = "TRADE"
    NO_TRADE = "NO_TRADE"


class OrderType(str, Enum):
    LIMIT = "LIMIT"  # market orders are not proposable


_TRADE_FIELDS = ("symbol", "side", "order_type", "limit_price", "requested_notional")


class ProposalBody(Contract):
    """Exactly what the planner LLM must return. Attribution is added by code, never by the model."""

    decision: Decision
    symbol: Optional[Symbol] = None
    side: Optional[Side] = None
    order_type: Optional[OrderType] = None
    limit_price: Optional[Decimal] = Field(default=None, gt=0)
    requested_notional: Optional[Decimal] = Field(default=None, gt=0)
    invalidation_conditions: List[ShortText] = Field(default_factory=list, max_length=10)
    evidence_refs: List[EvidenceRef] = Field(default_factory=list, max_length=50)
    public_rationale: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _shape(self):
        if self.decision is Decision.TRADE:
            missing = [f for f in _TRADE_FIELDS if getattr(self, f) is None]
            if missing:
                raise ValueError(f"TRADE requires {missing}")
            if not self.evidence_refs:
                raise ValueError("TRADE requires at least one evidence_ref")
            if not self.invalidation_conditions:
                raise ValueError("TRADE requires at least one invalidation_condition")
        else:
            present = [f for f in _TRADE_FIELDS if getattr(self, f) is not None]
            if present:
                raise ValueError(f"NO_TRADE must not set {present}")
        return self


class OrderProposal(ProposalBody):
    proposal_id: str
    market_snapshot_id: str
    account_snapshot_id: str
    model_version: str
    prompt_version: str
    prompt_sha256: str
    strategy_version: str
