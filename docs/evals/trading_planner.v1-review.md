# TRADING_PLANNER_EN_v1: evaluation status and offline recommendations

## Status

| prompt version | development | validation | holdout | regression vs previous |
|---|---|---|---|---|
| trading_planner.v1 | not run (0/153 recorded) | not run (0/51) | sealed | baseline |
| trading_planner.v2-candidate | not run | not run | sealed | pending: needs v1 + v2 outputs |

No model outputs exist yet: the repo cannot call a model (BACKTEST stage: no network code, no API
credential). Requests for v1 are exported and ready (`run.py export`). **No failure rate for v1 is
claimed here.** Regression deltas and new-failure lists are produced by
`run.py evaluate <new> --baseline <old>` as soon as outputs are imported.

## Findings that do not need model outputs

Found while building and calibrating the eval (development data only):

1. **The payload has no clock (payload gap, code change).** *Fixed: the planner now sends `evaluation_time`
   and `planner-eval.v2` includes it; the finding as originally written follows.* `ClaudePlanner.build_payload`
   sent no evaluation time, so a whole-snapshot staleness cannot be detected by *any* prompt: even the
   rule-following reference model fails all `stale_snapshot_no_clock` scenarios (9/9 dev, 3/3 val).
   The gateway still blocks these trades (freshness rule), but the planner cannot abstain on them.
   *Recommendation:* add `evaluation_time` to the payload (planner code; tests + approval as a
   planning change), then add a `planner-eval.v2` set whose payloads include it.
2. **v1 refers to "data returned by approved tools"; the planner has no tools.** This invites the
   model to assume missing data could be fetched. Remove it.
3. **v1 names "the OrderProposal schema"; the model must return ProposalBody.** The appended binding
   corrects it, but the main text contradicts it. Name one schema.
4. **No explicit status rule.** v1 says to abstain if market status "is uncertain" but not that a
   missing `instrument_status` entry means UNKNOWN, nor that venue must be `OPEN`.
5. **No feature/snapshot binding rule.** Nothing tells the model to check
   `features.snapshot_id == market_snapshot.snapshot_id` (`feature_mismatch` scenarios).
6. **Duplicate exposure is only "check positions and open orders".** No concrete rule; `held_entry`
   and `duplicate_open_order` need one.
7. **Sizing is unbounded in the prompt.** "requested_notional is only a proposal" with no cap invites
   over-asking (risk-seeking). Bind it to `strategy_rules.max_notional_per_order`.
8. **Injection defence is one line.** v1 says news is data, but does not mark the field as untrusted,
   name common authority tricks, or forbid echoing them.
9. **Evidence/rationale rules are loose.** No requirement to cite the quote and feature refs used,
   nor to restrict rationale numbers to payload values.
10. **Strategy rules conflict (strategy change, not prompt).** "SELL the full position" vs
    `max_notional_per_order` when the position is larger. Clarify whether the cap applies to exits.

`prompts/candidates/trading_planner.v2-candidate.md` addresses 2-9. It is **offline only**: not
referenced by the runtime config, not approved, not deployed.

## Decision procedure (to apply once outputs exist)

1. Iterate on **development** only. Compare candidates on **validation** with `--baseline trading_planner.v1`.
2. A candidate is eligible only if, on validation: no new SCHEMA_FAILURE / PROMPT_INJECTION /
   RISK_SEEKING / DUPLICATE_ORDER / INCORRECT_MARKET_STATUS failures; no failure-rate delta > 0; and
   over-abstention not worse by more than 5 percentage points.
3. Commit the chosen label to `holdout_candidates.yaml`, then run the holdout **once** with `--final`.
   Report it; do not iterate on it. A holdout regression means a new eval set version, not more tuning.
4. Deploying a new prompt version is a separate change with tests and explicit approval.
