"""Exposure arithmetic shared by the caps. Open BUY orders count as exposure (conservative);
open SELL orders never reduce it. Positions without a current quote cannot be marked: caps
then deny new risk instead of guessing a price."""
from decimal import Decimal
from typing import Dict, Optional

from trading_agent.core.types import Side
from trading_agent.risk.rules.context import INF, Cap, RuleContext, fail, headroom_cap, ok

TEN_K = Decimal(10000)


def marked_values(ctx: RuleContext) -> Optional[Dict[str, Decimal]]:
    """Signed market value per held symbol, or None if any position cannot be marked."""
    values = {}
    for sym, pos in ctx.account.positions.items():
        quote = ctx.market.quotes.get(sym)
        if quote is None:
            return None
        values[sym] = pos.quantity * quote.mid
    return values


def open_buys(ctx: RuleContext, symbol: Optional[str] = None, sector: Optional[str] = None) -> Decimal:
    total = Decimal(0)
    for o in ctx.account.open_orders:
        if o.side is not Side.BUY or (symbol is not None and o.symbol != symbol):
            continue
        if sector is not None and (o.symbol not in ctx.policy.instruments or ctx.policy.sector_of(o.symbol) != sector):
            continue
        total += o.notional
    return total


def equity(ctx: RuleContext) -> Optional[Decimal]:
    values = marked_values(ctx)
    return None if values is None else ctx.account.cash + sum(values.values(), Decimal(0))


def _not_applicable(rule_id: str) -> Cap:
    return Cap(INF, ok(rule_id, "risk-reducing or not applicable"))


def _unmarkable(rule_id: str) -> Cap:
    return Cap(Decimal(0), fail(rule_id, "UNMARKABLE_POSITION", "held position has no current quote"))


def gross_cap(ctx: RuleContext) -> Cap:
    if ctx.is_risk_reducing:
        return _not_applicable("gross_exposure")
    values = marked_values(ctx)
    if values is None:
        return _unmarkable("gross_exposure")
    used = sum((abs(v) for v in values.values()), Decimal(0)) + open_buys(ctx)
    return headroom_cap("gross_exposure", ctx.policy.max_gross_exposure, used, "GROSS_EXPOSURE_LIMIT")


def net_cap(ctx: RuleContext) -> Cap:
    if ctx.is_risk_reducing:
        return _not_applicable("net_exposure")
    values = marked_values(ctx)
    if values is None:
        return _unmarkable("net_exposure")
    used = sum(values.values(), Decimal(0)) + open_buys(ctx)
    return headroom_cap("net_exposure", ctx.policy.max_net_exposure, used, "NET_EXPOSURE_LIMIT")


def sector_cap(ctx: RuleContext) -> Cap:
    if ctx.is_risk_reducing:
        return _not_applicable("sector_exposure")
    values = marked_values(ctx)
    if values is None:
        return _unmarkable("sector_exposure")
    sector = ctx.policy.sector_of(ctx.symbol)
    used = sum((abs(v) for s, v in values.items()
                if s in ctx.policy.instruments and ctx.policy.sector_of(s) == sector), Decimal(0))
    used += open_buys(ctx, sector=sector)
    return headroom_cap("sector_exposure", ctx.policy.max_sector_exposure[sector], used, "SECTOR_EXPOSURE_LIMIT")


def concentration_cap(ctx: RuleContext) -> Cap:
    if ctx.is_risk_reducing:
        return _not_applicable("concentration")
    eq = equity(ctx)
    if eq is None:
        return _unmarkable("concentration")
    if eq <= 0:
        return Cap(Decimal(0), fail("concentration", "NON_POSITIVE_EQUITY", str(eq)))
    used = ctx.held_quantity * ctx.quote.mid + open_buys(ctx, symbol=ctx.symbol)
    return headroom_cap("concentration", ctx.policy.max_concentration_pct * eq, used, "CONCENTRATION_LIMIT")


def buying_power_cap(ctx: RuleContext) -> Cap:
    if ctx.proposal.side is not Side.BUY:
        return _not_applicable("buying_power")
    available = ctx.account.cash - open_buys(ctx) - ctx.policy.min_cash_buffer
    if available <= 0:
        return Cap(Decimal(0), fail("buying_power", "INSUFFICIENT_BUYING_POWER", str(available)))
    return Cap(available / (1 + ctx.policy.commission_bps / TEN_K), ok("buying_power", f"available {available}"))
