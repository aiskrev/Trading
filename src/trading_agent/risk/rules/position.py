from decimal import Decimal

from trading_agent.core.types import Side
from trading_agent.risk.rules.context import Cap, RuleContext, fail, headroom_cap, ok
from trading_agent.risk.rules.exposure import open_buys

RULE = "position"


def cap(ctx: RuleContext) -> Cap:
    held = ctx.held_quantity
    if ctx.proposal.side is Side.BUY:
        used = held * ctx.quote.mid + open_buys(ctx, symbol=ctx.symbol)
        return headroom_cap(RULE, ctx.policy.max_position_notional, used, "POSITION_LIMIT")
    if held <= 0:  # long-only at this stage
        return Cap(Decimal(0), fail(RULE, "SHORT_NOT_ALLOWED"))
    return Cap(held * ctx.proposal.limit_price, ok(RULE, "exit capped at held quantity"))
