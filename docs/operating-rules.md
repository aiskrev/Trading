# Operating rules for every evaluation

Implemented by `src/trading_agent/orchestration/evaluation_gate.py`, which runs **before the model**
in the backtest engine and the live loop. The first unmet prerequisite returns NO_TRADE with a
`reason_code` (recorded in the DecisionPacket and the DECISION audit record); the model is not called.

| # | rule | check | reason codes |
|---|---|---|---|
| 0 | operate only approved versions | runtime `strategy_version` / `risk_policy_version` equal the loaded ones and an approval covers them (strategy file + trigger code hashes, risk policy hashes) for the active mode | STRATEGY_NOT_APPROVED, RISK_POLICY_NOT_APPROVED |
| 1 | market-data freshness | quotes exist; snapshot and account within max age, not in the future | MARKET_DATA_UNKNOWN, MARKET_DATA_STALE, ACCOUNT_UNKNOWN, ACCOUNT_STALE |
| 2 | broker/account reconciliation | broker view present, fresh, positions equal our book | RECONCILIATION_UNKNOWN, RECONCILIATION_MISMATCH |
| 3 | mode and kill switch | mode permitted; kill switch readable and off; not HALTED; venue known and OPEN | MODE_NOT_PERMITTED, KILL_SWITCH_UNKNOWN, KILL_SWITCH_ACTIVE, TRADING_HALTED, MARKET_STATUS_UNKNOWN, MARKET_CLOSED |
| 4 | allowed universe | approved universe minus restricted list, quoted, instrument status TRADING | INSTRUMENT_STATUS_UNKNOWN, NO_ELIGIBLE_INSTRUMENT |
| 5 | deterministic trigger | strategy trigger active on an eligible symbol (`strategy/triggers.py`) | FEATURES_UNKNOWN, NO_TRIGGER |
| 6 | valid OrderProposal or NO_TRADE | model sees `active_triggers`; a TRADE not matching a trigger (symbol + side) is replaced by NO_TRADE; schema/model failures are NO_TRADE with the failure as reason | PROPOSAL_NOT_TRIGGERED, SCHEMA_FAILURE, LLM_UNAVAILABLE, MODEL_FAILURE |
| 7 | never submit directly | the planner cannot reach execution (import-linter); only gateway-signed orders execute | — |

Everything after the gate is unchanged: a triggered proposal still goes through the critic and the
deterministic risk gateway.

## Approved versions (config/approvals.yaml)

| approval | covers | modes |
|---|---|---|
| APR-0001 | risk-policy.v2 + restricted.v1 | BACKTEST |
| APR-0002 | simulator code + execution-assumptions.v1 | BACKTEST |
| APR-0003 | strategy mean-reversion-demo.v1 + trigger code | BACKTEST |

Editing an approved file removes its approval (hash mismatch); a new version needs a new entry.

## Kill switch

Create `ops/KILL_SWITCH` (or `<run_dir>/KILL_SWITCH` for the live loop) to stop new evaluations: the
next cycle returns NO_TRADE `KILL_SWITCH_ACTIVE` and trips HALTED. Nothing in the code creates or
removes the file; an unreadable state counts as UNKNOWN (NO_TRADE). Clearing a HALT for the next
session needs `halt_ack.json` (see docs/paper-trading-plan.md).
