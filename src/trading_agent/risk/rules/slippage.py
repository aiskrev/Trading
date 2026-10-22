from decimal import Decimal

from trading_agent.core.types import Side
from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "slippage"


def worst_case_cost_bps(ctx: RuleContext) -> Decimal:
    """Most we could pay versus mid if filled at our limit, plus commission."""
    mid = ctx.quote.mid
    signed = ctx.proposal.limit_price - mid if ctx.proposal.side is Side.BUY else mid - ctx.proposal.limit_price
    return signed / mid * Decimal(10000) + ctx.policy.commission_bps


def check(ctx: RuleContext):
    if ctx.quote is None:
        return fail(RULE, "NO_QUOTE")
    cost = worst_case_cost_bps(ctx)
    if cost > ctx.policy.max_slippage_bps:
        return fail(RULE, "SLIPPAGE_EXCEEDS_POLICY", f"{cost:.2f}bps > {ctx.policy.max_slippage_bps}bps")
    return ok(RULE)
