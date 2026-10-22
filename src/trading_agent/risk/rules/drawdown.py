from trading_agent.risk.rules.context import RuleContext, fail, ok
from trading_agent.risk.rules.exposure import equity

RULE = "drawdown"


def check(ctx: RuleContext):
    """Mark-to-market drawdown from the intraday equity high-water. Unknown => no new risk."""
    if ctx.is_risk_reducing:
        return ok(RULE, "exit allowed")
    hw = ctx.account.equity_high_water
    if hw is None:
        return fail(RULE, "DRAWDOWN_UNKNOWN", "no equity high-water mark")
    eq = equity(ctx)
    if eq is None:
        return fail(RULE, "UNMARKABLE_POSITION", "cannot mark equity")
    dd = (hw - eq) / hw
    if dd >= ctx.policy.max_drawdown_pct:
        return fail(RULE, "DRAWDOWN_LIMIT", f"{dd:.4%} >= {ctx.policy.max_drawdown_pct:.2%}")
    return ok(RULE)
