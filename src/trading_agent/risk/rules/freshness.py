from datetime import timedelta

from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "freshness"


def check(ctx: RuleContext):
    """Snapshot freshness: market and account snapshots must be recent and not from the future."""
    max_age = timedelta(seconds=ctx.policy.max_snapshot_age_seconds)
    skew = timedelta(seconds=ctx.policy.max_future_skew_seconds)
    for name, as_of in (("market", ctx.market.as_of), ("account", ctx.account.as_of)):
        if as_of - ctx.now > skew:
            return fail(RULE, "CLOCK_SKEW", f"{name} snapshot is in the future")
        if ctx.now - as_of > max_age:
            return fail(RULE, "STALE_DATA", f"{name} snapshot age {ctx.now - as_of}")
    return ok(RULE)


def check_quote(ctx: RuleContext):
    """Quote freshness for the traded instrument (stricter than snapshot age)."""
    quote = ctx.quote
    if quote is None:
        return fail("quote_freshness", "NO_QUOTE")
    if quote.ts - ctx.now > timedelta(seconds=ctx.policy.max_future_skew_seconds):
        return fail("quote_freshness", "CLOCK_SKEW", "quote is in the future")
    age = ctx.now - quote.ts
    if age > timedelta(seconds=ctx.policy.max_quote_age_seconds):
        return fail("quote_freshness", "STALE_QUOTE", f"quote age {age}")
    return ok("quote_freshness")
