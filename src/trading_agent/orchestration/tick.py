"""One decision cycle: snapshot -> plan -> critique -> risk -> (audit) -> execute -> audit -> reconcile.

Ordering guarantees:
  * the DECISION record is written BEFORE any submission (write-ahead); if it fails we HALT;
  * nothing reaches execution except a gateway-minted ApprovedOrder;
  * every fault path is audited and escalates TradingState per fail_closed.py.
"""
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Dict, List, NamedTuple, Optional

from typing_extensions import Protocol

from trading_agent.audit.ledger import AuditLedger
from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.audit import Attribution, AuditEventType
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.execution import AckStatus, ApprovedOrder, Fill
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import Decision, OrderProposal
from trading_agent.contracts.risk import RiskDecision
from trading_agent.core.clock import Clock
from trading_agent.core.errors import AuditWriteFailure, DuplicateOrder, ForgedOrder
from trading_agent.core.mode import RuntimeConfig
from trading_agent.core.trading_state import TradingState, TradingStateMachine
from trading_agent.execution.ports import ExecutionPort, ExecutionResult
from trading_agent.normalization.validators import DataIssue
from trading_agent.observability.metrics import Metrics
from trading_agent.orchestration.fail_closed import CONSECUTIVE_REJECTS_BEFORE_REDUCE_ONLY, escalation_for
from trading_agent.planning.critic import ClaudeCritic
from trading_agent.planning.planner import ClaudePlanner
from trading_agent.reconciliation.reconciler import Reconciler
from trading_agent.risk.gateway import RiskGateway


class Book(Protocol):
    def mark(self, market: MarketSnapshot) -> None: ...
    def snapshot(self, as_of) -> AccountSnapshot: ...
    def apply_fill(self, fill: Fill) -> None: ...
    def record_order(self) -> None: ...
    def quantities(self) -> Dict[str, Decimal]: ...


class Trace:
    """Per-tick observability: stage timings, raw model text, account used, state before/after."""

    def __init__(self, state_before: TradingState, data_issues: List[DataIssue]) -> None:
        self.state_before = state_before
        self.state_after = state_before
        self.data_issues = data_issues
        self.timings_ms: Dict[str, float] = {}
        self.signal_to_proposal_ms: Optional[float] = None
        self.account: Optional[AccountSnapshot] = None
        self.planner_raw: Optional[str] = None
        self.planner_failure: Optional[str] = None
        self.critic_raw: Optional[str] = None
        self.gate_reason: Optional[str] = None

    @contextmanager
    def timed(self, stage: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.timings_ms[stage] = round((time.perf_counter() - t0) * 1000, 3)


class TickResult(NamedTuple):
    proposal: Optional[OrderProposal] = None
    critic: Optional[CriticVerdict] = None
    decision: Optional[RiskDecision] = None
    order: Optional[ApprovedOrder] = None
    execution: Optional[ExecutionResult] = None
    skipped: Optional[str] = None
    trace: Optional[Trace] = None


class TradingPipeline:
    def __init__(self, config: RuntimeConfig, planner: ClaudePlanner, critic: Optional[ClaudeCritic],
                 gateway: RiskGateway, execution: ExecutionPort, ledger: AuditLedger,
                 state: TradingStateMachine, reconciler: Reconciler, book: Book, clock: Clock,
                 metrics: Optional[Metrics] = None, gate=None) -> None:
        self.config = config
        self.gate = gate  # EvaluationGate; None only in unit-test harnesses that probe the gateway directly
        self.planner = planner
        self.critic = critic
        self.gateway = gateway
        self.execution = execution
        self.ledger = ledger
        self.state = state
        self.reconciler = reconciler
        self.book = book
        self.clock = clock
        self.metrics = metrics or Metrics()
        self._consecutive_rejects = 0

    # ------------------------------------------------------------------ public
    def tick(self, market: MarketSnapshot, features: FeatureSet, data_issues: List[DataIssue] = ()) -> TickResult:
        trace = Trace(state_before=self.state.state, data_issues=list(data_issues))
        started = time.perf_counter()
        try:
            result = self._tick(market, features, data_issues, trace)
        finally:
            trace.timings_ms["total"] = round((time.perf_counter() - started) * 1000, 3)
            trace.state_after = self.state.state
        return result._replace(trace=trace)

    def _tick(self, market: MarketSnapshot, features: FeatureSet, data_issues: List[DataIssue],
              trace: "Trace") -> TickResult:
        now = self.clock.now()
        self.metrics.inc("ticks")
        if data_issues:
            self.metrics.inc("data_issues", len(data_issues))
            self._audit(AuditEventType.DATA_FAULT, market, details={
                "reason": ";".join(f"{i.code.value}:{i.symbol or '-'}" for i in data_issues)[:1000]})
            esc = escalation_for(data_issues)
            if esc:
                self._trip(esc[0], esc[1], market)
        if market.as_of > now:
            self._trip(TradingState.HALTED, "CLOCK_SKEW:snapshot_after_clock", market)
        if self.state.state is TradingState.HALTED:
            self.metrics.inc("ticks_skipped_halted")
            return TickResult(skipped="HALTED")

        self.book.mark(market)
        account = self.book.snapshot(now)
        trace.account = account
        broker = BrokerAccountView(source_id="execution", as_of=now, positions=self.execution.broker_positions())
        gate = None
        if self.gate is not None:
            with trace.timed("gate"):
                gate = self.gate.check(market, account, broker, features, self.state.state, now)
            trace.gate_reason = gate.reason_code
            if gate.reason_code == "KILL_SWITCH_ACTIVE":
                self._trip(TradingState.HALTED, "KILL_SWITCH", market)
        if gate is not None and not gate.ok:
            # A prerequisite is not met (or nothing triggered): NO_TRADE with a reason, model not consulted.
            self.metrics.inc(f"gate_{gate.reason_code.lower()}")
            proposal = self.planner.no_trade(market, account, gate.reason_code, gate.detail)
        else:
            hits = gate.triggers if gate is not None else None
            with trace.timed("planner"):
                plan = self.planner.plan(market, account, features, _trigger_context(hits))
            trace.planner_raw, trace.planner_failure = plan.raw_output, plan.failure
            proposal = plan.proposal
            if plan.failure:
                self.metrics.inc(f"planner_{plan.failure.lower()}")
                if not self._audit(AuditEventType.SCHEMA_FAILURE, market, proposal=proposal,
                                   details={"reason": plan.failure}):
                    return TickResult(proposal=proposal, skipped="AUDIT_FAILURE")
            if hits is not None and proposal.decision is Decision.TRADE and \
                    (proposal.symbol, proposal.side.value) not in {(h.symbol, h.side) for h in hits}:
                # the model may only act on what the deterministic trigger selected
                self.metrics.inc("gate_proposal_not_triggered")
                trace.gate_reason = "PROPOSAL_NOT_TRIGGERED"
                proposal = self.planner.no_trade(market, account, "PROPOSAL_NOT_TRIGGERED",
                                                 f"model proposed {proposal.side.value} {proposal.symbol}; "
                                                 f"active triggers {[(h.symbol, h.side) for h in hits]}")

        critic = None
        if proposal.decision is Decision.TRADE and self.critic is not None:
            with trace.timed("critic"):
                critic = self.critic.review(proposal, market, account, self.gateway.policy.summary())
            trace.critic_raw = self.critic.last_raw
        with trace.timed("risk"):
            result = self.gateway.evaluate(proposal, critic, market, account, self.state.state, features, broker)
        # signal = newest quote in the snapshot; latency = its age when the proposal exists (+ model time)
        signal_ts = max((q.ts for q in market.quotes.values()), default=market.as_of)
        trace.signal_to_proposal_ms = round((self.clock.now() - signal_ts).total_seconds() * 1000
                                            + trace.timings_ms.get("planner", 0.0), 3)
        self.metrics.inc(f"risk_{result.decision.verdict.value.lower()}")
        details = {"proposal_id": proposal.proposal_id}
        if proposal.reason_code:
            details["reason_code"] = proposal.reason_code
        if proposal.symbol and proposal.symbol in market.quotes:
            details["decision_mid"] = str(market.quotes[proposal.symbol].mid)
        # Write-ahead: the decision is durable before anything can be submitted.
        if not self._audit(AuditEventType.DECISION, market, proposal=proposal, critic=critic,
                           risk_decision=result.decision,
                           approved_order=result.order.signing_payload() if result.order else None,
                           broker_result="PENDING_SUBMIT" if result.order else "NOT_SUBMITTED", details=details):
            return TickResult(proposal, critic, result.decision, skipped="AUDIT_FAILURE")
        if result.order is None:
            return TickResult(proposal, critic, result.decision)

        with trace.timed("execution"):
            execution = self._submit(result.order, market, proposal)
        self._reconcile(market)
        return TickResult(proposal, critic, result.decision, result.order, execution)

    def record_event(self, event_type: AuditEventType, market: MarketSnapshot, details: Dict[str, str]) -> bool:
        """Audit a non-decision event (e.g. a corporate action applied to the book)."""
        return self._audit(event_type, market, details=details)

    # ----------------------------------------------------------------- helpers
    def _submit(self, order: ApprovedOrder, market: MarketSnapshot, proposal: OrderProposal):
        try:
            execution = self.execution.submit(order)
        except (ForgedOrder, DuplicateOrder) as exc:
            reason = getattr(exc, "reason_code", None)
            self._trip(TradingState.HALTED, f"{type(exc).__name__}:{reason}" if reason else type(exc).__name__,
                       market)
            return None
        except Exception as exc:  # unknown broker state => no new risk
            self._trip(TradingState.HALTED, f"BROKER_ERROR:{type(exc).__name__}", market)
            return None
        self.book.record_order()
        for fill in execution.fills:
            self.book.apply_fill(fill)
        broker_result = _broker_result(execution, order)
        self.metrics.inc(f"broker_{broker_result.lower()}")
        self._audit(AuditEventType.EXECUTION, market, proposal=proposal, acks=[execution.ack],
                    fills=execution.fills, broker_result=broker_result,
                    details={"proposal_id": proposal.proposal_id, "order_id": order.order_id})
        if execution.ack.status is AckStatus.REJECTED:
            self._consecutive_rejects += 1
            if self._consecutive_rejects >= CONSECUTIVE_REJECTS_BEFORE_REDUCE_ONLY:
                self._trip(TradingState.REDUCE_ONLY, "CONSECUTIVE_BROKER_REJECTS", market)
        else:
            self._consecutive_rejects = 0
        return execution

    def _reconcile(self, market: MarketSnapshot) -> None:
        report = self.reconciler.reconcile(self.book.quantities(), self.execution.broker_positions())
        if not report.matched:
            self.metrics.inc("reconciliation_mismatch")
            self._audit(AuditEventType.RECONCILIATION, market, details={
                "reason": "POSITION_MISMATCH",
                "mismatches": ";".join(f"{m.symbol}:{m.internal}!={m.broker}" for m in report.mismatches)[:1000]})
            self._trip(TradingState.HALTED, "RECONCILIATION_MISMATCH", market)

    def _trip(self, to: TradingState, reason: str, market: MarketSnapshot) -> None:
        before = self.state.state
        if self.state.trip(to, reason, self.clock.now()):
            self.metrics.inc(f"state_{to.value.lower()}")
            self._audit(AuditEventType.STATE_CHANGE, market,
                        details={"reason": reason, "from": before.value, "to": to.value})

    def _audit(self, event_type: AuditEventType, market: MarketSnapshot, proposal: Optional[OrderProposal] = None,
               broker_result: str = "NOT_SUBMITTED", **fields: Any) -> bool:
        attribution = Attribution(
            model_version=proposal.model_version if proposal else self.config.model_version,
            prompt_version=proposal.prompt_version if proposal else _prompt_versions(self.config),
            strategy_version=self.config.strategy_version,
            market_snapshot_id=market.snapshot_id,
            risk_policy_version=self.gateway.policy.version,
            code_commit=self.config.code_commit,
            deployment_id=self.config.deployment_id,
            broker_result=broker_result,
        )
        try:
            self.ledger.append(at=self.clock.now(), event_type=event_type, trading_state=self.state.state,
                               attribution=attribution, proposal=proposal, **fields)
            return True
        except AuditWriteFailure:
            # Cannot audit => cannot trade. Escalate directly (the escalation itself cannot be audited).
            self.metrics.inc("audit_write_failure")
            self.state.trip(TradingState.HALTED, "AUDIT_WRITE_FAILURE", self.clock.now())
            return False


def _trigger_context(hits) -> Optional[List[Dict[str, str]]]:
    if hits is None:
        return None
    return [{"symbol": h.symbol, "trigger": h.kind.value, "side": h.side, "feature_value": str(h.feature_value)}
            for h in hits]


def _broker_result(execution: ExecutionResult, order: ApprovedOrder) -> str:
    if execution.ack.status is not AckStatus.ACCEPTED:
        return execution.ack.status.value
    if not execution.fills:
        return "ACCEPTED_NO_FILL_REPORTED"
    filled = sum(f.quantity * f.price for f in execution.fills)
    return "FILLED" if filled >= order.notional * Decimal("0.95") else "PARTIAL"


def _prompt_versions(config: RuntimeConfig) -> str:
    return ",".join(f"{k}.{v}" for k, v in sorted(config.prompt_versions.items()))
