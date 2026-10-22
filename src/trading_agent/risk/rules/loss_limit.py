from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "loss_limit"


def check(ctx: RuleContext):
    # Losses never raise limits (no martingale / revenge trading); past the limit only exits are allowed.
    if ctx.account.realized_pnl_today <= -ctx.policy.daily_loss_limit and not ctx.is_risk_reducing:
        return fail(RULE, "DAILY_LOSS_LIMIT", str(ctx.account.realized_pnl_today))
    return ok(RULE)
