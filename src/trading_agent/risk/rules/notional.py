from trading_agent.risk.rules.context import Cap, RuleContext, ok


def cap(ctx: RuleContext) -> Cap:
    return Cap(ctx.policy.max_order_notional, ok("max_order_notional"))
