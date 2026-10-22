from decimal import Decimal

from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "price_collar"


def check(ctx: RuleContext):
    quote = ctx.quote
    if quote is None:
        return fail(RULE, "NO_QUOTE")
    dev_bps = abs(ctx.proposal.limit_price - quote.mid) / quote.mid * Decimal(10000)
    if dev_bps > ctx.policy.price_collar_bps:
        return fail(RULE, "PRICE_OUTSIDE_COLLAR", f"{dev_bps:.2f}bps from mid")
    return ok(RULE)
