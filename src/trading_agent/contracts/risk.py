from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import Field, field_validator, model_validator

from trading_agent.contracts.base import Contract, require_aware


class RiskVerdict(str, Enum):
    APPROVE = "APPROVE"
    REDUCE = "REDUCE"
    DENY = "DENY"


class RuleResult(Contract):
    rule_id: str
    passed: bool
    reason_code: Optional[str] = None
    detail: str = ""


class RiskDecision(Contract):
    decision_id: str
    proposal_id: str
    verdict: RiskVerdict
    requested_notional: Decimal = Field(ge=0)
    approved_notional: Decimal = Field(ge=0)
    rule_results: List[RuleResult]
    policy_version: str
    policy_sha256: str
    evaluated_at: datetime

    _aware = field_validator("evaluated_at")(require_aware)

    @model_validator(mode="after")
    def _invariants(self):
        if self.approved_notional > self.requested_notional:
            raise ValueError("approved_notional may never exceed requested_notional")
        if self.verdict is RiskVerdict.DENY and self.approved_notional != 0:
            raise ValueError("DENY must approve zero notional")
        if self.verdict is not RiskVerdict.DENY and self.approved_notional == 0:
            raise ValueError("APPROVE/REDUCE must approve positive notional")
        return self

    @property
    def reason_codes(self) -> List[str]:
        return [r.reason_code for r in self.rule_results if not r.passed and r.reason_code]
