from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "duplicate"


def check(ctx: RuleContext):
    clash = [o.order_id for o in ctx.account.open_orders if o.symbol == ctx.proposal.symbol]
    if clash:
        return fail(RULE, "DUPLICATE_OR_CONFLICTING_ORDER", ",".join(clash))
    return ok(RULE)
