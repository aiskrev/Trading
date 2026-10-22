from decimal import Decimal

from trading_agent.contracts.base import Contract
from trading_agent.contracts.market import Quote
from trading_agent.core.types import Side

BPS = Decimal(10000)


class CostModel(Contract):
    """Approved transaction-cost model shared by the simulator and expected-edge calculations."""

    version: str = "costs.v1"
    commission_bps: Decimal = Decimal("1")
    slippage_bps: Decimal = Decimal("2")

    def fill_price(self, side: Side, quote: Quote, limit_price: Decimal) -> Decimal:
        if side is Side.BUY:
            return min(quote.ask * (1 + self.slippage_bps / BPS), limit_price)
        return max(quote.bid * (1 - self.slippage_bps / BPS), limit_price)

    def fee(self, notional: Decimal) -> Decimal:
        return (notional * self.commission_bps / BPS).quantize(Decimal("0.01"))
