# Pre-trade Risk Gateway

`src/trading_agent/risk/gateway.py` is the only component that can mint an executable
(`ApprovedOrder`, HMAC-signed) order. It is deterministic: same inputs, same policy, same clock
⇒ same decision. Changes here need tests and an explicit production approval (CLAUDE.md, CODEOWNERS).

## Threshold provenance: configuration only, never Claude

* Every threshold lives in `config/risk_policy.vN.yaml` (+ `restricted.vN.yaml`).
* Released files are **immutable**: their SHA-256 is pinned in `config/risk_config.lock.yaml`.
  `load_risk_policy` refuses an edited, unpinned, or mis-named file. To change a limit, publish a
  new version and pin it with `scripts/lock_risk_config.py` (which refuses to re-pin a changed release).
* The runtime config names the policy version it expects; startup fails on mismatch.
* **Approval is enforced.** `config/approvals.yaml` (append-only, CODEOWNERS) binds each approval to
  the pinned hashes and to specific modes. `BacktestEngine` refuses to start unless the loaded policy
  has an approval for the current mode (`risk/approvals.py`). APR-0001 approves risk-policy.v2 +
  restricted.v1 for BACKTEST only.
* `RiskPolicy` is a frozen model handed to the gateway once. `RiskGateway` exposes only
  `evaluate`, `policy` (read-only) and `mode`. Every `RiskDecision` records `policy_version` and
  `policy_sha256`.
* Claude's planner output is parsed into `ProposalBody` (`extra="forbid"`): any threshold-like field
  is a schema failure ⇒ NO_TRADE. The critic receives a *copy* of the policy and can only tighten
  (`REDUCE` is an upper bound, `DENY` is final).
* Rule modules contain no numeric thresholds (AST-checked); every `policy.<attr>` they read must be a
  declared policy field (checked).

## Controls

Gate rules (any failure ⇒ DENY; a rule that raises or returns garbage ⇒ DENY with `RULE_ERROR`):

| Control | Rule | Reason codes |
|---|---|---|
| Mode / trading state | `mode`, `state` | MODE_NOT_PERMITTED, TRADING_HALTED, REDUCE_ONLY_STATE |
| Proposal shape & snapshot binding | `schema` | NOT_A_TRADE, ORDER_TYPE_NOT_ALLOWED, *_SNAPSHOT_MISMATCH |
| Allowed instrument universe | `universe` | OUTSIDE_UNIVERSE |
| Broker/account consistency | `broker_consistency` | BROKER_VIEW_MISSING, BROKER_VIEW_STALE, BROKER_ACCOUNT_MISMATCH |
| Snapshot freshness | `freshness.check` | STALE_DATA, CLOCK_SKEW |
| Quote freshness | `freshness.check_quote` | NO_QUOTE, STALE_QUOTE, CLOCK_SKEW |
| Session state (calendar, hours, holidays, open/close windows) | `session` | OUTSIDE_TRADING_SESSION |
| Venue state | `market_state` | MARKET_NOT_OPEN, MARKET_STATE_UNKNOWN |
| Halt / restricted instrument | `instrument_status` | RESTRICTED_INSTRUMENT, INSTRUMENT_HALTED, INSTRUMENT_STATUS_UNKNOWN |
| Evidence binding | `evidence` | UNRESOLVED_EVIDENCE, MISSING_QUOTE_EVIDENCE |
| Price collar | `price_collar` | PRICE_OUTSIDE_COLLAR |
| Spread policy | `spread` | SPREAD_TOO_WIDE |
| Slippage policy (side-adjusted limit vs mid + commission) | `slippage` | SLIPPAGE_EXCEEDS_POLICY |
| Duplicate / open-order conflicts | `duplicate` | DUPLICATE_OR_CONFLICTING_ORDER |
| Daily realized loss | `loss_limit` | DAILY_LOSS_LIMIT |
| Mark-to-market drawdown from high-water | `drawdown` | DRAWDOWN_LIMIT, DRAWDOWN_UNKNOWN, UNMARKABLE_POSITION |
| Order rates (day, minute, instrument-minute) | `rate` | ORDER_RATE_LIMIT, ORDER_RATE_LIMIT_MINUTE, INSTRUMENT_RATE_LIMIT_MINUTE |
| Critic | `critic` | CRITIC_MISSING, CRITIC_DENY, CRITIC_PROPOSAL_MISMATCH |

Caps (approved size = min of all; any cap at zero ⇒ DENY; result < min notional ⇒ DENY unless exiting):

| Control | Cap | Reason codes |
|---|---|---|
| Max order notional | `notional` | — |
| Max position exposure (incl. open buys) | `position` | POSITION_LIMIT, SHORT_NOT_ALLOWED |
| Gross exposure | `exposure.gross_cap` | GROSS_EXPOSURE_LIMIT |
| Net exposure | `exposure.net_cap` | NET_EXPOSURE_LIMIT |
| Sector exposure | `exposure.sector_cap` | SECTOR_EXPOSURE_LIMIT |
| Single-name concentration (% of equity) | `exposure.concentration_cap` | CONCENTRATION_LIMIT, NON_POSITIVE_EQUITY |
| Buying power (cash − open buys − buffer) | `exposure.buying_power_cap` | INSUFFICIENT_BUYING_POWER |
| Critic REDUCE | `critic.cap` | — |

Exits (risk-reducing sells of a long) bypass exposure caps, loss and drawdown gates, and are
allowed in REDUCE_ONLY. They are **not** exempt from: broker consistency, freshness, session,
venue, halts/restricted list, collar, spread, slippage, rate limits, critic.

## Assumptions to review

* Backtest fixtures have no halt data; `BacktestEngine` declares quoted symbols TRADING by default.
* The equity high-water is tracked by the internal book; live accounts must supply it, else
  DRAWDOWN_UNKNOWN denies new risk.
* The holiday list in `risk_policy.v2.yaml` covers 2026 only and must be extended by a new version.

## Evidence (tests)

* `tests/invariants/test_risk_invariants.py`: independent oracle (`risk_oracle.py`) over a broad
  fault-heavy state space and a near-valid sizing space; one targeted denial per control; exits;
  stateful rate windows; monotonicity (loss, exposure, critic); fail-closed rule errors; signing.
* `tests/invariants/test_claude_cannot_bypass.py`: fuzzed planner/critic JSON and raw text through the
  full pipeline; every threshold field rejected; critic mutating its view; gateway surface; no
  hard-coded thresholds; config files unchanged.
* `tests/unit/test_risk_config.py`: tamper/unpinned/mis-named config refused; lock tool refuses
  re-pin; decision carries pinned hash; runtime/policy version agreement; session calendar incl. DST.
* Mutation checks run during development: disabling sector cap, slippage, broker position check,
  session, latency or gap handling each fails at least one test.
