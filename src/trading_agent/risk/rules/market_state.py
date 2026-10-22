from trading_agent.contracts.market import VenueStatus
from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "market_state"


def check(ctx: RuleContext):
    status = ctx.market.venue_status
    if status is VenueStatus.UNKNOWN:
        return fail(RULE, "MARKET_STATE_UNKNOWN")
    if status is not VenueStatus.OPEN:
        return fail(RULE, "MARKET_NOT_OPEN", status.value)
    return ok(RULE)
