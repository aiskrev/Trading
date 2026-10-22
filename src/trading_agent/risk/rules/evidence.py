from trading_agent.risk.rules.context import RuleContext, fail, ok

RULE = "evidence"


def check(ctx: RuleContext):
    known = ctx.market.evidence_refs() | ctx.account.evidence_refs()
    if ctx.features is not None:
        known |= ctx.features.evidence_refs()
    refs = set(ctx.proposal.evidence_refs)
    unresolved = sorted(refs - known)
    if unresolved:
        return fail(RULE, "UNRESOLVED_EVIDENCE", ",".join(unresolved)[:200])
    if f"quote:{ctx.proposal.symbol}" not in refs:
        return fail(RULE, "MISSING_QUOTE_EVIDENCE")
    return ok(RULE)
