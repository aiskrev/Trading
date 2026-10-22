from datetime import timedelta

from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "rate"
WINDOW = timedelta(minutes=1)


def check(ctx: RuleContext):
    if ctx.account.orders_today >= ctx.policy.max_orders_per_day:
        return fail(RULE, "ORDER_RATE_LIMIT", f"{ctx.account.orders_today} today")
    recent = [(t, s) for t, s in ctx.recent_orders if ctx.now - t < WINDOW]
    if len(recent) >= ctx.policy.max_orders_per_minute:
        return fail(RULE, "ORDER_RATE_LIMIT_MINUTE", f"{len(recent)} in last minute")
    same = sum(1 for _, s in recent if s == ctx.symbol)
    if same >= ctx.policy.max_orders_per_instrument_per_minute:
        return fail(RULE, "INSTRUMENT_RATE_LIMIT_MINUTE", f"{same} for {ctx.symbol} in last minute")
    return ok(RULE)
