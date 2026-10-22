from decimal import Decimal
from enum import Enum
from typing import List, Optional

from pydantic import Field, model_validator
from typing_extensions import Annotated

from trading_agent.contracts.base import Contract
from trading_agent.core.types import EvidenceRef

ReasonCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]


class CriticVerdictKind(str, Enum):
    PASS = "PASS"
    REDUCE = "REDUCE"
    DENY = "DENY"


class CriticBody(Contract):
    """What the critic LLM must return. It has no field that could add size, flip side or change symbol."""

    verdict: CriticVerdictKind
    max_notional: Optional[Decimal] = Field(default=None, gt=0)
    reason_codes: List[ReasonCode] = Field(default_factory=list, max_length=20)
    evidence_refs: List[EvidenceRef] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _shape(self):
        if self.verdict is CriticVerdictKind.REDUCE and self.max_notional is None:
            raise ValueError("REDUCE requires max_notional")
        if self.verdict is not CriticVerdictKind.REDUCE and self.max_notional is not None:
            raise ValueError("max_notional only allowed with REDUCE")
        return self


class CriticVerdict(CriticBody):
    proposal_id: str
    model_version: str
    prompt_version: str
