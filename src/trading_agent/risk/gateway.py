from collections import deque
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Callable, Deque, List, NamedTuple, Optional, Sequence, Tuple

from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.execution import ApprovedOrder
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import Decision, OrderProposal
from trading_agent.contracts.risk import RiskDecision, RiskVerdict, RuleResult
from trading_agent.core.clock import Clock
from trading_agent.core.mode import Mode
from trading_agent.core.signing import OrderSigner
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import IdFactory
from trading_agent.risk.policy import RiskPolicy
from trading_agent.risk.rules import CAPS, GATE_RULES
from trading_agent.risk.rules.context import Cap, RuleContext, fail

CENT = Decimal("0.01")
RATE_WINDOW = timedelta(minutes=1)


class GatewayResult(NamedTuple):
    decision: RiskDecision
    order: Optional[ApprovedOrder]


class RiskGateway:
    """The single, deterministic authority that can turn a proposal into an executable order.

    Invariants (tests/invariants):
      * every threshold comes from the RiskPolicy given at construction (immutable, versioned,
        hash-pinned); nothing in a proposal or critic verdict can change it;
      * approved_notional <= requested_notional; symbol and side are never changed;
      * any failed or raising rule => DENY; the critic can only tighten; a critic DENY is final;
      * only this class holds the signer used to mint ApprovedOrder.
    """

    def __init__(self, policy: RiskPolicy, mode: Mode, signer: OrderSigner, ids: IdFactory, clock: Clock,
                 gate_rules: Sequence[Callable[[RuleContext], RuleResult]] = GATE_RULES,
                 caps: Sequence[Callable[[RuleContext], Cap]] = CAPS) -> None:
        if not isinstance(policy, RiskPolicy):
            raise TypeError("policy must be a loaded RiskPolicy")
        self._policy = policy
        self._mode = mode
        self._signer = signer
        self._ids = ids
        self._clock = clock
        self._gate_rules = tuple(gate_rules)
        self._caps = tuple(caps)
        self._recent: Deque[Tuple[datetime, str]] = deque()

    @property
    def policy(self) -> RiskPolicy:
        return self._policy

    @property
    def mode(self) -> Mode:
        return self._mode

    def evaluate(self, proposal: OrderProposal, critic: Optional[CriticVerdict], market: MarketSnapshot,
                 account: AccountSnapshot, trading_state: TradingState, features: Optional[FeatureSet] = None,
                 broker: Optional[BrokerAccountView] = None) -> GatewayResult:
        now = self._clock.now()
        requested = proposal.requested_notional or Decimal(0)
        if proposal.decision is Decision.NO_TRADE:
            return self._deny(proposal, requested, [fail("schema", "NO_TRADE_PROPOSED")], now)

        while self._recent and now - self._recent[0][0] >= RATE_WINDOW:
            self._recent.popleft()
        ctx = RuleContext(proposal, critic, market, account, features, self._policy, self._mode, trading_state,
                          now, broker, tuple(self._recent))
        results: List[RuleResult] = [self._run_rule(rule, ctx) for rule in self._gate_rules]
        if any(not r.passed for r in results):
            return self._deny(proposal, requested, results, now)

        approved = requested
        for cap_fn in self._caps:
            try:
                cap = cap_fn(ctx)
                if not isinstance(cap, Cap) or not isinstance(cap.limit, Decimal) or cap.limit.is_nan():
                    cap = Cap(Decimal(0), fail(_name(cap_fn), "RULE_ERROR", "cap returned invalid value"))
            except Exception as exc:
                cap = Cap(Decimal(0), fail(_name(cap_fn), "RULE_ERROR", type(exc).__name__))
            results.append(cap.result)
            approved = min(approved, cap.limit)
        approved = max(approved, Decimal(0)).quantize(CENT, rounding=ROUND_DOWN)

        if approved < self._policy.min_order_notional and not ctx.is_risk_reducing:
            results.append(fail("min_notional", "BELOW_MIN_NOTIONAL", str(approved)))
        if approved <= 0:
            results.append(fail("headroom", "NO_HEADROOM"))
        if any(not r.passed for r in results):
            return self._deny(proposal, requested, results, now)

        verdict = RiskVerdict.APPROVE if approved == requested else RiskVerdict.REDUCE
        decision = self._decision(proposal, verdict, requested, approved, results, now)
        order = self._mint(decision, proposal, approved)
        self._recent.append((now, proposal.symbol))
        return GatewayResult(decision, order)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _run_rule(rule: Callable[[RuleContext], RuleResult], ctx: RuleContext) -> RuleResult:
        try:
            result = rule(ctx)
            if not isinstance(result, RuleResult):
                return fail(_name(rule), "RULE_ERROR", "rule returned non-RuleResult")
            return result
        except Exception as exc:  # fail closed
            return fail(_name(rule), "RULE_ERROR", type(exc).__name__)

    def _deny(self, proposal, requested, results, now) -> GatewayResult:
        return GatewayResult(self._decision(proposal, RiskVerdict.DENY, requested, Decimal(0), results, now), None)

    def _decision(self, proposal: OrderProposal, verdict: RiskVerdict, requested: Decimal, approved: Decimal,
                  results: List[RuleResult], now) -> RiskDecision:
        return RiskDecision(decision_id=self._ids.next("risk"), proposal_id=proposal.proposal_id, verdict=verdict,
                            requested_notional=requested, approved_notional=approved, rule_results=results,
                            policy_version=self._policy.version, policy_sha256=self._policy.source_sha256,
                            evaluated_at=now)

    def _mint(self, decision: RiskDecision, proposal: OrderProposal, approved: Decimal) -> ApprovedOrder:
        unsigned = ApprovedOrder(order_id=self._ids.next("order"), decision_id=decision.decision_id,
                                 proposal_id=proposal.proposal_id, symbol=proposal.symbol, side=proposal.side,
                                 limit_price=proposal.limit_price, notional=approved,
                                 issued_at=decision.evaluated_at, signature="")
        return unsigned.model_copy(update={"signature": self._signer.sign(unsigned.signing_payload())})


def _name(fn) -> str:
    return f"{getattr(fn, '__module__', '?').rsplit('.', 1)[-1]}.{getattr(fn, '__name__', '?')}"
