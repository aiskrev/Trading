from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import Field

from trading_agent.contracts.account import AccountSnapshot
from trading_agent.contracts.audit import Attribution
from trading_agent.contracts.base import Contract
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.data_quality import DataIssue
from trading_agent.contracts.execution import BrokerAck, Fill
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import OrderProposal
from trading_agent.contracts.risk import RiskDecision
from trading_agent.core.trading_state import TradingState


class DecisionPacket(Contract):
    """Everything needed to replay and audit one decision cycle, written for EVERY cycle
    (TRADE, NO_TRADE, schema failure, skipped while halted). Raw model text is kept verbatim."""

    packet_id: str
    session_id: str
    run_segment: str                       # changes on every restart of the same run
    at: datetime                           # decision clock time
    outcome: str                           # NO_TRADE | DENIED | APPROVED | REDUCED | SKIPPED_HALTED | ...
    attribution: Attribution
    state_before: TradingState
    state_after: TradingState
    market: MarketSnapshot
    features: Optional[FeatureSet] = None
    account: Optional[AccountSnapshot] = None
    data_issues: List[DataIssue] = Field(default_factory=list)
    planner_raw_output: Optional[str] = None
    planner_failure: Optional[str] = None
    proposal: Optional[OrderProposal] = None
    critic_raw_output: Optional[str] = None
    critic: Optional[CriticVerdict] = None
    risk_decision: Optional[RiskDecision] = None
    approved_order: Optional[Dict[str, Any]] = None   # signature never stored
    acks: List[BrokerAck] = Field(default_factory=list)
    fills: List[Fill] = Field(default_factory=list)
    timings_ms: Dict[str, float] = Field(default_factory=dict)
    signal_to_proposal_ms: Optional[float] = None
    errors: List[str] = Field(default_factory=list)
