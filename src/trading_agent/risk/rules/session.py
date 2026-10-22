from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "session"


def check(ctx: RuleContext):
    """Trading calendar from policy (weekdays, hours, holidays, open/close avoidance windows)."""
    if not ctx.policy.session.is_open(ctx.now):
        return fail(RULE, "OUTSIDE_TRADING_SESSION", ctx.now.isoformat())
    return ok(RULE)
