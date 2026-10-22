"""Deterministic post-trade auditor (the 9 sections of TRADE_AUDITOR_EN_v1).

Every conclusion cites evidence IDs = audit record hashes. Lessons are candidates for
OFFLINE review only; nothing here can modify strategy or risk configuration.
"""
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field

from trading_agent.audit.ledger import AuditLedger
from trading_agent.contracts.audit import AuditEventType, AuditRecord
from trading_agent.contracts.base import Contract
from trading_agent.core.types import Side

BPS = Decimal(10000)
Q = Decimal("0.0001")


class TradeAudit(Contract):
    proposal_id: str
    evidence_ids: List[str]
    proposed_action: str
    supporting_facts: List[str]
    triggered_risk_rules: List[str]
    actual_execution: str
    slippage_bps: Optional[Decimal] = None
    implementation_shortfall: Optional[Decimal] = None
    expected_vs_realized: str
    invalidation_condition_occurred: str
    operational_anomalies: List[str] = Field(default_factory=list)
    candidate_lessons_offline: List[str] = Field(default_factory=list)


class AuditReport(Contract):
    ledger_verified: bool
    ledger_head: str
    trades: List[TradeAudit]
    run_anomalies: List[str]


def build_report(records: List[AuditRecord]) -> AuditReport:
    executions: Dict[str, AuditRecord] = {r.details["proposal_id"]: r for r in records
                                          if r.event_type is AuditEventType.EXECUTION and "proposal_id" in r.details}
    trades: List[TradeAudit] = []
    for rec in records:
        if rec.event_type is not AuditEventType.DECISION or rec.proposal is None:
            continue
        p, d = rec.proposal, rec.risk_decision
        if p.decision.value == "NO_TRADE" or d is None:
            continue
        ex = executions.get(p.proposal_id)
        evidence = [rec.record_hash] + ([ex.record_hash] if ex else [])
        anomalies, lessons = [], []
        slip = shortfall = None
        execution = "NOT_SUBMITTED"
        realized = "No order submitted; no position change expected or observed."
        if ex is not None:
            fills = ex.fills
            execution = f"{ex.attribution.broker_result}: {len(fills)} fill(s)"
            mid = Decimal(rec.details["decision_mid"]) if "decision_mid" in rec.details else None
            if fills and mid is not None:
                qty = sum(f.quantity for f in fills)
                avg = sum(f.quantity * f.price for f in fills) / qty
                sign = Decimal(1) if p.side is Side.BUY else Decimal(-1)
                slip = (sign * (avg - mid) / mid * BPS).quantize(Q)
                shortfall = (sign * (avg - mid) * qty + sum(f.fee for f in fills)).quantize(Q)
                filled_notional = (qty * avg).quantize(Decimal("0.01"))
                realized = f"Approved notional {d.approved_notional}; filled notional {filled_notional}."
                if filled_notional < d.approved_notional * Decimal("0.95"):
                    anomalies.append("PARTIAL_FILL")
                    lessons.append("Review liquidity assumptions for partial fills (offline).")
            elif not fills:
                anomalies.append(f"NO_FILL:{ex.attribution.broker_result}")
        trades.append(TradeAudit(
            proposal_id=p.proposal_id,
            evidence_ids=evidence,
            proposed_action=f"{p.side.value} {p.symbol} notional {p.requested_notional} @ limit {p.limit_price}",
            supporting_facts=list(p.evidence_refs) + [f"market_snapshot:{p.market_snapshot_id}"],
            triggered_risk_rules=[f"{r.rule_id}:{r.reason_code}" for r in d.rule_results if not r.passed]
            + [f"verdict:{d.verdict.value}"],
            actual_execution=execution,
            slippage_bps=slip,
            implementation_shortfall=shortfall,
            expected_vs_realized=realized,
            invalidation_condition_occurred="NOT_EVALUATED: conditions are free text at this stage",
            operational_anomalies=anomalies,
            candidate_lessons_offline=lessons,
        ))
    run_anomalies = [f"{r.event_type.value}:{r.record_hash}:{r.details.get('reason', '')}" for r in records
                     if r.event_type in (AuditEventType.SCHEMA_FAILURE, AuditEventType.DATA_FAULT,
                                         AuditEventType.STATE_CHANGE, AuditEventType.RECONCILIATION)]
    return AuditReport(ledger_verified=AuditLedger.verify(records),
                       ledger_head=records[-1].record_hash if records else "", trades=trades,
                       run_anomalies=run_anomalies)
