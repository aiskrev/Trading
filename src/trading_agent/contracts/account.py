from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Set

from pydantic import Field, ValidationInfo, field_validator, model_validator

from trading_agent.contracts.base import Contract, build_with_content_id, computing_id, content_id, require_aware
from trading_agent.core.types import Side, Symbol


class Position(Contract):
    symbol: Symbol
    quantity: Decimal
    avg_price: Decimal = Field(ge=0)


class OpenOrder(Contract):
    order_id: str
    symbol: Symbol
    side: Side
    notional: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)


class AccountSnapshot(Contract):
    snapshot_id: str
    as_of: datetime
    cash: Decimal
    positions: Dict[Symbol, Position] = Field(default_factory=dict)
    open_orders: List[OpenOrder] = Field(default_factory=list)
    realized_pnl_today: Decimal = Decimal(0)
    orders_today: int = Field(default=0, ge=0)
    equity_high_water: Optional[Decimal] = Field(default=None, gt=0)  # intraday peak; None => drawdown unknown

    _aware = field_validator("as_of")(require_aware)

    @model_validator(mode="after")
    def _integrity(self, info: ValidationInfo) -> "AccountSnapshot":
        if not computing_id(info) and self.snapshot_id != content_id(self, "snapshot_id"):
            raise ValueError("snapshot_id does not match content hash")
        for sym, pos in self.positions.items():
            if pos.symbol != sym:
                raise ValueError(f"position key {sym} != position.symbol {pos.symbol}")
        return self

    @classmethod
    def build(cls, **fields) -> "AccountSnapshot":
        return build_with_content_id(cls, "snapshot_id", **fields)

    def quantity(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.quantity if pos else Decimal(0)

    def evidence_refs(self) -> Set[str]:
        refs = {"account:cash", "account:pnl"}
        refs.update(f"position:{s}" for s in self.positions)
        refs.update(f"order:{o.order_id}" for o in self.open_orders)
        return refs


class BrokerAccountView(Contract):
    """What the broker itself reports, used to check our internal book before every order."""

    source_id: str = Field(min_length=1, max_length=64)
    as_of: datetime
    positions: Dict[Symbol, Decimal] = Field(default_factory=dict)
    open_order_count: Optional[int] = Field(default=None, ge=0)

    _aware = field_validator("as_of")(require_aware)
