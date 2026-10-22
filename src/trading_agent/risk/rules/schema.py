from trading_agent.contracts.proposal import Decision, OrderType
from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "schema"


def check(ctx: RuleContext):
    p = ctx.proposal
    if p.decision is not Decision.TRADE or p.symbol is None:
        return fail(RULE, "NOT_A_TRADE")
    if p.order_type is not OrderType.LIMIT:
        return fail(RULE, "ORDER_TYPE_NOT_ALLOWED")
    if p.market_snapshot_id != ctx.market.snapshot_id:
        return fail(RULE, "MARKET_SNAPSHOT_MISMATCH")
    if p.account_snapshot_id != ctx.account.snapshot_id:
        return fail(RULE, "ACCOUNT_SNAPSHOT_MISMATCH")
    return ok(RULE)
