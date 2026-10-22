from decimal import Decimal
from typing import Dict

from trading_agent.contracts.account import AccountSnapshot, Position
from trading_agent.contracts.execution import Fill
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.core.types import Side


class Portfolio:
    """Internal book of record for the backtest. Reconciled against the broker after every order."""

    def __init__(self, starting_cash: Decimal) -> None:
        self.cash = starting_cash
        self.realized_pnl = Decimal(0)
        self.orders = 0
        self.equity_high_water = starting_cash
        self._qty: Dict[str, Decimal] = {}
        self._avg: Dict[str, Decimal] = {}

    def apply_fill(self, fill: Fill) -> None:
        qty, avg = self._qty.get(fill.symbol, Decimal(0)), self._avg.get(fill.symbol, Decimal(0))
        if fill.side is Side.BUY:
            new_qty = qty + fill.quantity
            self._avg[fill.symbol] = (qty * avg + fill.quantity * fill.price) / new_qty
            self._qty[fill.symbol] = new_qty
            self.cash -= fill.notional + fill.fee
        else:
            self.realized_pnl += (fill.price - avg) * fill.quantity
            self._qty[fill.symbol] = qty - fill.quantity
            self.cash += fill.notional - fill.fee
        self.realized_pnl -= fill.fee
        if self._qty[fill.symbol] == 0:
            del self._qty[fill.symbol]
            self._avg.pop(fill.symbol, None)

    def mark(self, market: MarketSnapshot) -> None:
        """Raise the equity high-water if every position can be marked from this snapshot."""
        if any(s not in market.quotes for s in self._qty):
            return
        equity = self.cash + sum((q * market.quotes[s].mid for s, q in self._qty.items()), Decimal(0))
        self.equity_high_water = max(self.equity_high_water, equity)

    def record_order(self) -> None:
        self.orders += 1

    def quantities(self) -> Dict[str, Decimal]:
        return dict(self._qty)

    def snapshot(self, as_of) -> AccountSnapshot:
        positions = {s: Position(symbol=s, quantity=q, avg_price=self._avg[s]) for s, q in sorted(self._qty.items())}
        return AccountSnapshot.build(as_of=as_of, cash=self.cash, positions=positions, open_orders=[],
                                     realized_pnl_today=self.realized_pnl, orders_today=self.orders,
                                     equity_high_water=self.equity_high_water)
