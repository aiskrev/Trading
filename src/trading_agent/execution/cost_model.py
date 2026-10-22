from decimal import Decimal
from pathlib import Path
from typing import Union

import yaml
from pydantic import Field, ValidationError

from trading_agent.contracts.base import Contract
from trading_agent.contracts.market import Quote
from trading_agent.core.errors import ConfigError
from trading_agent.core.types import Side

BPS = Decimal(10000)
CENT = Decimal("0.01")


class CostModel(Contract):
    """Approved transaction-cost model shared by the simulator and reporting.

    Fills happen at the observed touch (ask for buys, bid for sells), so the quoted spread is paid
    from market data; `slippage_bps` is additional, configurable impact on top of the touch.
    """

    version: str = "costs.v1"
    commission_bps: Decimal = Field(default=Decimal("1"), ge=0)
    min_commission: Decimal = Field(default=Decimal("0"), ge=0)   # per fill
    sell_fee_bps: Decimal = Field(default=Decimal("0"), ge=0)     # regulatory/exchange fees on sells
    slippage_bps: Decimal = Field(default=Decimal("2"), ge=0)

    def fill_price(self, side: Side, quote: Quote, limit_price: Decimal) -> Decimal:
        if side is Side.BUY:
            return min(quote.ask * (1 + self.slippage_bps / BPS), limit_price)
        return max(quote.bid * (1 - self.slippage_bps / BPS), limit_price)

    def fee(self, notional: Decimal, side: Side = Side.BUY) -> Decimal:
        commission = max(self.min_commission, notional * self.commission_bps / BPS)
        regulatory = notional * self.sell_fee_bps / BPS if side is Side.SELL else Decimal(0)
        return (commission + regulatory).quantize(CENT)

    def scaled(self, factor: Decimal) -> "CostModel":
        """Same model with every modelled cost multiplied (used for cost-sensitivity runs)."""
        return CostModel(version=f"{self.version}x{factor}", commission_bps=self.commission_bps * factor,
                         min_commission=self.min_commission * factor, sell_fee_bps=self.sell_fee_bps * factor,
                         slippage_bps=self.slippage_bps * factor)


class ExecutionAssumptions(Contract):
    """Simulator assumptions for BACKTEST. Versioned config: config/execution_assumptions.vN.yaml."""

    version: str
    costs: CostModel
    latency_ms: int = Field(ge=0, le=60000)           # order reaches the market this long after the decision
    partial_fills: bool                               # fill only up to displayed size; IOC remainder cancelled
    max_participation: Decimal = Field(gt=0, le=1)    # share of displayed size we may take


def load_execution_assumptions(path: Union[str, Path]) -> ExecutionAssumptions:
    try:
        return ExecutionAssumptions.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigError(f"invalid execution assumptions {path}: {exc}") from exc
