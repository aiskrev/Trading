from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "spread"


def check(ctx: RuleContext):
    quote = ctx.market.quotes.get(ctx.proposal.symbol)
    if quote is None:
        return fail(RULE, "NO_QUOTE")
    if quote.spread_bps > ctx.policy.max_spread_bps:
        return fail(RULE, "SPREAD_TOO_WIDE", f"{quote.spread_bps:.2f}bps")
    return ok(RULE)
