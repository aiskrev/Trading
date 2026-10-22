from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "universe"


def check(ctx: RuleContext):
    if ctx.proposal.symbol not in ctx.policy.universe:
        return fail(RULE, "OUTSIDE_UNIVERSE", str(ctx.proposal.symbol))
    return ok(RULE)
