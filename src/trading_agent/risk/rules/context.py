from datetime import datetime
from decimal import Decimal
from typing import NamedTuple, Optional, Tuple

from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot, Quote
from trading_agent.contracts.proposal import OrderProposal
from trading_agent.contracts.risk import RuleResult
from trading_agent.core.mode import Mode
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import Side
from trading_agent.risk.policy import RiskPolicy

INF = Decimal("Infinity")


class RuleContext(NamedTuple):
    """Everything a rule may look at. Thresholds come only from `policy`; `proposal` and
    `critic` are model output and are treated purely as the thing being checked."""

    proposal: OrderProposal
    critic: Optional[CriticVerdict]
    market: MarketSnapshot
    account: AccountSnapshot
    features: Optional[FeatureSet]
    policy: RiskPolicy
    mode: Mode
    trading_state: TradingState
    now: datetime
    broker: Optional[BrokerAccountView] = None
    recent_orders: Tuple[Tuple[datetime, str], ...] = ()  # (approved_at, symbol), kept by the gateway

    @property
    def symbol(self) -> str:
        return self.proposal.symbol

    @property
    def quote(self) -> Optional[Quote]:
        return self.market.quotes.get(self.proposal.symbol)

    @property
    def held_quantity(self) -> Decimal:
        return self.account.quantity(self.proposal.symbol)

    @property
    def is_risk_reducing(self) -> bool:
        held = self.held_quantity
        return (self.proposal.side is Side.SELL and held > 0) or (self.proposal.side is Side.BUY and held < 0)


class Cap(NamedTuple):
    limit: Decimal
    result: RuleResult


def ok(rule_id: str, detail: str = "") -> RuleResult:
    return RuleResult(rule_id=rule_id, passed=True, detail=detail)


def fail(rule_id: str, reason_code: str, detail: str = "") -> RuleResult:
    return RuleResult(rule_id=rule_id, passed=False, reason_code=reason_code, detail=detail[:300])


def headroom_cap(rule_id: str, limit: Decimal, used: Decimal, code: str) -> Cap:
    room = limit - used
    if room <= 0:
        return Cap(Decimal(0), fail(rule_id, code, f"used {used} of {limit}"))
    return Cap(room, ok(rule_id, f"headroom {room}"))
