from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import Field, field_validator, model_validator

from trading_agent.contracts.account import AccountSnapshot, OpenOrder, Position
from trading_agent.contracts.base import Contract, require_aware
from trading_agent.core.types import Side, Symbol


class PositionRecord(Contract):
    instrument: Symbol
    vendor_symbol: str = Field(max_length=32)
    quantity: Decimal
    avg_price: Decimal = Field(ge=0)


class OpenOrderRecord(Contract):
    order_ref: str = Field(min_length=1, max_length=64)
    instrument: Symbol
    side: Side
    notional: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)


class AccountRecord(Contract):
    """Account state from one source with explicit provenance and timestamps.

    `account_alias` is a configured label; the broker's account number is never stored.
    """

    source_id: str = Field(min_length=1, max_length=64)
    account_alias: str = Field(pattern=r"^[a-z0-9_\-]{1,32}$")
    source_ts: datetime
    received_at: datetime
    version: Optional[int] = Field(default=None, ge=0)
    cash: Decimal
    realized_pnl_today: Decimal
    orders_today: int = Field(ge=0)
    positions: Dict[Symbol, PositionRecord] = Field(default_factory=dict)
    open_orders: List[OpenOrderRecord] = Field(default_factory=list)

    _aware = field_validator("source_ts", "received_at")(require_aware)

    @model_validator(mode="after")
    def _keys(self):
        for sym, pos in self.positions.items():
            if pos.instrument != sym:
                raise ValueError("position key does not match instrument")
        return self

    def to_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot.build(
            as_of=self.source_ts, cash=self.cash, realized_pnl_today=self.realized_pnl_today,
            orders_today=self.orders_today,
            positions={s: Position(symbol=s, quantity=p.quantity, avg_price=p.avg_price)
                       for s, p in sorted(self.positions.items()) if p.quantity != 0},
            open_orders=[OpenOrder(order_id=o.order_ref, symbol=o.instrument, side=o.side, notional=o.notional,
                                   limit_price=o.limit_price) for o in self.open_orders],
        )
