from trading_agent.core.mode import PERMITTED_MODES_THIS_STAGE
from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "mode"


def check(ctx: RuleContext):
    if ctx.mode not in PERMITTED_MODES_THIS_STAGE:
        return fail(RULE, "MODE_NOT_PERMITTED", ctx.mode.value)
    return ok(RULE)
