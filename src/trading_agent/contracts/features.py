from decimal import Decimal
from typing import Dict, Set

from trading_agent.contracts.base import Contract
from trading_agent.core.types import Symbol


class FeatureSet(Contract):
    snapshot_id: str
    feature_version: str
    values: Dict[Symbol, Dict[str, Decimal]]

    def evidence_refs(self) -> Set[str]:
        return {f"feature:{sym}:{name}" for sym, feats in self.values.items() for name in feats}
