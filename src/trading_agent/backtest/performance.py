"""Gross and net-of-cost performance from a BacktestReport.

net   = what the simulated account actually earned (touch prices, slippage, commissions, fees).
gross = the same fills valued at the mid price with no fees: net equity + cumulative costs.
"""
import math
from decimal import Decimal
from typing import Dict, List, Optional

from trading_agent.backtest.engine import BacktestReport
from trading_agent.contracts.base import Contract

D0 = Decimal(0)


class Performance(Contract):
    basis: str                       # "gross" | "net"
    start_equity: Decimal
    end_equity: Decimal
    pnl: Decimal
    total_return: float
    max_drawdown: float
    daily_mean_return: float
    daily_volatility: float
    sharpe_annualized: Optional[float]   # None when volatility is zero or < 2 days


class CostBreakdown(Contract):
    traded_notional: Decimal
    spread_and_slippage: Decimal
    fees: Decimal
    total: Decimal
    cost_bps_of_traded: float


class Summary(Contract):
    days: int
    fills: int
    partial_fills: int
    orders_expired: int
    turnover: float
    dividends: Decimal
    gross: Performance
    net: Performance
    costs: CostBreakdown


def _perf(basis: str, start: Decimal, series: List[Decimal]) -> Performance:
    curve = [start] + series
    rets = [float(b / a - 1) for a, b in zip(curve, curve[1:]) if a]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, float((peak - v) / peak) if peak else 0.0)
    mean = sum(rets) / len(rets) if rets else 0.0
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else 0.0
    return Performance(basis=basis, start_equity=start, end_equity=curve[-1], pnl=curve[-1] - start,
                       total_return=float(curve[-1] / start - 1), max_drawdown=mdd, daily_mean_return=mean,
                       daily_volatility=vol, sharpe_annualized=(mean / vol * math.sqrt(252)) if vol > 0 else None)


def summarize(r: BacktestReport) -> Summary:
    cum_cost_by_day: Dict = {}
    running = D0
    costs_by_day: Dict = {}
    for c in r.fill_costs:
        costs_by_day[c.day] = costs_by_day.get(c.day, D0) + c.spread_and_slippage + c.fees
    for m in r.daily:
        running += costs_by_day.get(m.day, D0)
        cum_cost_by_day[m.day] = running
    net_series = [m.equity for m in r.daily]
    gross_series = [m.equity + cum_cost_by_day[m.day] for m in r.daily]
    spread = sum((c.spread_and_slippage for c in r.fill_costs), D0)
    fees = sum((c.fees for c in r.fill_costs), D0)
    traded = sum((c.notional for c in r.fill_costs), D0)
    approved = {f.order_id: f for f in r.fills}
    partial = sum(1 for t in r.audit.trades if "PARTIAL_FILL" in t.operational_anomalies)
    return Summary(
        days=len(r.daily), fills=len(r.fills), partial_fills=partial,
        orders_expired=r.metrics.get("broker_expired", 0),
        turnover=float(traded / r.starting_cash) if r.starting_cash else 0.0, dividends=r.dividends,
        gross=_perf("gross", r.starting_cash, gross_series), net=_perf("net", r.starting_cash, net_series),
        costs=CostBreakdown(traded_notional=traded, spread_and_slippage=spread, fees=fees, total=spread + fees,
                            cost_bps_of_traded=float((spread + fees) / traded * 10000) if traded else 0.0),
    )
