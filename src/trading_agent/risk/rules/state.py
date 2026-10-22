from trading_agent.core.trading_state import TradingState
from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "trading_state"


def check(ctx: RuleContext):
    if ctx.trading_state is TradingState.HALTED:
        return fail(RULE, "TRADING_HALTED")
    if ctx.trading_state is TradingState.REDUCE_ONLY and not ctx.is_risk_reducing:
        return fail(RULE, "REDUCE_ONLY_STATE", "only risk-reducing orders allowed")
    return ok(RULE)
