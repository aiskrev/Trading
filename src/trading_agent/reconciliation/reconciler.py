from decimal import Decimal
from typing import Dict, List

from pydantic import Field

from trading_agent.contracts.base import Contract


class PositionMismatch(Contract):
    symbol: str
    internal: Decimal
    broker: Decimal


class ReconciliationReport(Contract):
    matched: bool
    mismatches: List[PositionMismatch] = Field(default_factory=list)


class Reconciler:
    """Compares our internal book with the broker's. Any mismatch => HALTED (see fail_closed)."""

    def __init__(self, tolerance: Decimal = Decimal("0.000001")) -> None:
        self.tolerance = tolerance

    def reconcile(self, internal: Dict[str, Decimal], broker: Dict[str, Decimal]) -> ReconciliationReport:
        mismatches = []
        for sym in sorted(set(internal) | set(broker)):
            a, b = internal.get(sym, Decimal(0)), broker.get(sym, Decimal(0))
            if abs(a - b) > self.tolerance:
                mismatches.append(PositionMismatch(symbol=sym, internal=a, broker=b))
        return ReconciliationReport(matched=not mismatches, mismatches=mismatches)
