from decimal import Decimal
from typing import Dict, List, NamedTuple

from typing_extensions import Protocol

from trading_agent.contracts.execution import ApprovedOrder, BrokerAck, Fill


class ExecutionResult(NamedTuple):
    ack: BrokerAck
    fills: List[Fill]


class ExecutionPort(Protocol):
    """Deliberately narrow. There is no method that accepts a proposal, raw symbol/side/qty, or a dict."""

    def submit(self, order: ApprovedOrder) -> ExecutionResult:
        """Raises ForgedOrder for a bad signature and DuplicateOrder for a reused order_id."""

    def cancel(self, order_id: str) -> BrokerAck: ...

    def broker_positions(self) -> Dict[str, Decimal]:
        """Read-only broker-side view, used only by reconciliation."""
