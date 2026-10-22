from decimal import Decimal
from typing import Optional

from trading_agent.contracts.data_quality import DataIssue, DataIssueCode
from trading_agent.market_data.ports import RawTick

__all__ = ["DataIssue", "DataIssueCode", "validate_tick", "mid", "divergence_bps"]


def validate_tick(tick: RawTick) -> Optional[DataIssue]:
    if min(tick.bid, tick.ask, tick.last) <= 0 or min(tick.bid_size, tick.ask_size) < 0:
        return DataIssue(code=DataIssueCode.BAD_PRICE, symbol=tick.symbol, detail=f"source={tick.source_id}")
    if tick.bid > tick.ask:
        return DataIssue(code=DataIssueCode.CROSSED_QUOTE, symbol=tick.symbol, detail=f"bid={tick.bid} ask={tick.ask}")
    return None


def mid(tick: RawTick) -> Decimal:
    return (tick.bid + tick.ask) / 2


def divergence_bps(a: RawTick, b: RawTick) -> Decimal:
    ma, mb = mid(a), mid(b)
    return abs(ma - mb) / ((ma + mb) / 2) * Decimal(10000)
