from datetime import timedelta
from decimal import Decimal

from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "broker_consistency"


def check(ctx: RuleContext):
    """Our internal book must agree with the broker's own view before any order (exits included)."""
    b = ctx.broker
    if b is None:
        return fail(RULE, "BROKER_VIEW_MISSING") if ctx.policy.require_broker_view else ok(RULE, "not required")
    if ctx.now - b.as_of > timedelta(seconds=ctx.policy.max_broker_view_age_seconds):
        return fail(RULE, "BROKER_VIEW_STALE", f"age {ctx.now - b.as_of}")
    if b.as_of - ctx.now > timedelta(seconds=ctx.policy.max_future_skew_seconds):
        return fail(RULE, "CLOCK_SKEW", "broker view is in the future")
    tol = ctx.policy.position_tolerance
    for sym in sorted(set(b.positions) | set(ctx.account.positions)):
        ours, theirs = ctx.account.quantity(sym), b.positions.get(sym, Decimal(0))
        if abs(ours - theirs) > tol:
            return fail(RULE, "BROKER_ACCOUNT_MISMATCH", f"{sym}: internal {ours} broker {theirs}")
    if b.open_order_count is not None and b.open_order_count != len(ctx.account.open_orders):
        return fail(RULE, "BROKER_ACCOUNT_MISMATCH",
                    f"open orders: internal {len(ctx.account.open_orders)} broker {b.open_order_count}")
    return ok(RULE)
