from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from trading_agent.contracts.base import Contract
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.execution import BrokerAck, Fill
from trading_agent.contracts.proposal import OrderProposal
from trading_agent.contracts.risk import RiskDecision
from trading_agent.core.trading_state import TradingState

NonEmpty = Field(min_length=1)


class AuditEventType(str, Enum):
    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    DATA_FAULT = "DATA_FAULT"
    STATE_CHANGE = "STATE_CHANGE"
    RECONCILIATION = "RECONCILIATION"


class Attribution(Contract):
    """The eight attribution fields required by CLAUDE.md, all mandatory and non-empty."""

    model_version: str = NonEmpty
    prompt_version: str = NonEmpty
    strategy_version: str = NonEmpty
    market_snapshot_id: str = NonEmpty
    risk_policy_version: str = NonEmpty
    code_commit: str = NonEmpty
    deployment_id: str = NonEmpty
    broker_result: str = NonEmpty  # e.g. NOT_SUBMITTED, ACCEPTED, REJECTED, FILLED, PARTIAL


class AuditRecord(Contract):
    seq: int = Field(ge=0)
    at: datetime
    event_type: AuditEventType
    trading_state: TradingState
    attribution: Attribution
    proposal: Optional[OrderProposal] = None
    critic: Optional[CriticVerdict] = None
    risk_decision: Optional[RiskDecision] = None
    approved_order: Optional[Dict[str, Any]] = None  # signature excluded: it is a transient capability
    acks: List[BrokerAck] = Field(default_factory=list)
    fills: List[Fill] = Field(default_factory=list)
    details: Dict[str, str] = Field(default_factory=dict)
    prev_hash: str
    record_hash: str
