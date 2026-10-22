from decimal import Decimal

from trading_agent.contracts.critic import CriticVerdictKind
from trading_agent.risk.rules.context import Cap, RuleContext, fail, ok

RULE = "critic"


def check(ctx: RuleContext):
    c = ctx.critic
    if c is None:
        return fail(RULE, "CRITIC_MISSING") if ctx.policy.critic_required else ok(RULE)
    if c.proposal_id != ctx.proposal.proposal_id:
        return fail(RULE, "CRITIC_PROPOSAL_MISMATCH")
    if c.verdict is CriticVerdictKind.DENY:
        return fail(RULE, "CRITIC_DENY", ",".join(c.reason_codes)[:200])
    return ok(RULE)


def cap(ctx: RuleContext) -> Cap:
    # The critic can only shrink size; its number is an upper bound, never a target.
    c = ctx.critic
    if c is not None and c.verdict is CriticVerdictKind.REDUCE:
        return Cap(c.max_notional, ok("critic_cap"))
    return Cap(Decimal("Infinity"), ok("critic_cap"))
