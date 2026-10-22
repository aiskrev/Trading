from trading_agent.contracts.market import InstrumentStatus
from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "instrument_status"


def check(ctx: RuleContext):
    """Compliance restricted list (config) and exchange halt state (market snapshot). Blocks exits too."""
    sym = ctx.symbol
    if sym in ctx.policy.restricted:
        return fail(RULE, "RESTRICTED_INSTRUMENT", f"{sym} on {ctx.policy.restricted_list_version}")
    status = ctx.market.status_of(sym)
    if status is InstrumentStatus.HALTED:
        return fail(RULE, "INSTRUMENT_HALTED", sym)
    if status is InstrumentStatus.UNKNOWN and ctx.policy.require_instrument_status:
        return fail(RULE, "INSTRUMENT_STATUS_UNKNOWN", sym)
    return ok(RULE)
