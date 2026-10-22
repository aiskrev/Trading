# Roadmap: BACKTEST to LIMITED_LIVE

Status: **Proposed**, supersedes the vendor and ordering choices in `docs/paper-trading-plan.md` once
ADR 0002 is accepted. Architecture: ADR 0002. Key design: ADR 0003.

Rules for every phase:

* **Exit = every acceptance test passes and the owner approves**, recorded in `config/approvals.yaml`
  (hash-bound, per mode). Nothing in a phase changes the mode; only the owner does.
* A phase touching `/risk`, `/execution`, credentials, live deployment or broker permissions needs tests
  plus an explicit approval (CLAUDE.md).
* A failed acceptance test is fixed in that phase; it is never waived by moving on.
* Owner-only actions are marked **[owner]**.

```
P0 done        P1 decisions     P2 model       P3 data        P4 SHADOW
BACKTEST  -->  ADR approval --> Sonnet 5.5 --> feeds       --> live data,
skeleton       + CLAUDE.md      adapter+evals   (dev + prod)   no orders
                                                                  |
P9 CANARY_LIVE <-- P8 failover <-- P7 observ. <-- P6 PAPER <-- P5 key separation
   then LIMITED_LIVE                                        (risk service, Alpaca Paper)
```

(P5 deliberately precedes any broker credential; this reorders the earlier "adapter first" idea.)

## P0: BACKTEST skeleton (done)

Evidence: 415 tests, approvals APR-0001/2/3, gateway, adapters (mock), DecisionPackets, live loop on mock feeds.

## P1: Decisions **[owner]**

Deliverables: ADR 0002 and 0003 accepted or amended; CLAUDE.md "Testing" clarified (promotion into
a mode that sends orders to a broker needs paper tests); mode semantics table adopted; answers to the
ADR 0003 open questions.

Acceptance: ADRs marked Accepted with the owner's statement; CLAUDE.md amendment committed by the
owner; `tests/unit` check that mode semantics table in docs matches `core/mode.py` (to be added).

## P2: Model adapter and evaluation

**Status (APR-0007, BACKTEST): offline part done.** Built: `model_client` (tools-free request schema, pinned model,
bounded retries, strict reply validation, metadata-only logs; acceptance tests 1, 2 and 5 against a fake SDK, the
log part of 3 and the import-linter part of 3), `evaluation_time` in the planner payload, and the frozen eval set
`planner-eval.v2`. **Transport built, not yet run against the real API:** `AnthropicMessagesTransport` (Messages API,
key only from a `SecretsProvider`, no tools, allow-listed in `test_no_network_code`) and `scripts/run_model_eval.py`
(owner runs it in a terminal that holds the vault token; prints a cost ceiling and asks for YES). **Open:** the first real
call (owner: key into the vault, Python >= 3.10, `pip install anthropic`), and acceptance test 4 (scoring
`trading_planner.v1` and the v2 candidate on development and validation). See `docs/model-client.md`.

Deliverables: `LLMClient` implementation over the Agent SDK for Sonnet 5.5, **tools disabled**,
structured output, timeout and retry policy; recorded-response mode for replay; `evaluation_time` added
to the planner payload; planner eval set v2 (with clock) frozen.

Acceptance tests:
1. Adapter test with a fake SDK: no tool definitions are ever sent; any tool-use block in a reply is a
   schema failure => NO_TRADE.
2. Timeout, rate-limit, 5xx and malformed replies each map to the existing fail-closed codes.
3. API key only via `SecretsProvider`; secret-scan and log-redaction tests pass; adapter absent from
   the `execution` import graph (import-linter).
4. Planner quality eval on development and validation splits for Sonnet 5.5 and the candidate prompt
   (`docs/evals`): no new SCHEMA_FAILURE, PROMPT_INJECTION, RISK_SEEKING, DUPLICATE_ORDER or
   INCORRECT_MARKET_STATUS vs the baseline; holdout used once, last.
5. Replay of recorded responses is byte-identical.

Gate: **[owner]** supplies the model API key via the secrets manager; approval for the new prompt version.

## P3: Market-data feeds

Deliverables: vendor transport plug-in(s) implementing `StreamTransport` (Massive WebSocket and/or
Databento); production instrument registry and universe; risk policy v3 with real symbols and
sectors; dataset loader for recorded sessions; **development-only** Massive MCP tooling outside `src/`.

Acceptance tests:
1. Contract tests against recorded vendor payloads: malformed, delayed, duplicated, out-of-order,
   gap, reconnect, rate-limit (existing adapter test suite, run against the real decoder).
2. Trading-status/halt messages map to `InstrumentStatus`; absence => UNKNOWN => NO_TRADE.
3. Network code exists only in the transport package; `test_no_network_code` updated with an explicit,
   narrow allow-list; MCP code is not importable from `src/`.
4. Credentials read-only, from secrets manager; redaction tests; no secret in recorded fixtures.
5. Registry symbols equal the policy universe; sector limits present for every symbol.

Gate: **[owner]** vendor account, read-only key, data licence check; approval of risk policy v3.

## P4: REPLAY and SHADOW

Deliverables: REPLAY mode (recorded live feed with original timing); SHADOW mode (live data, full
pipeline, DecisionPackets persisted, **no order path**; hypothetical fills from later quotes); shadow
report; first durable store for packets.

Acceptance tests:
1. SHADOW cannot construct an execution port (registry test) and no broker credential is configured.
2. REPLAY of a recorded session reproduces decisions and ledger hash chain.
3. 10 consecutive SHADOW sessions: 100% of cycles have a packet; hash chain verifies; every NO_TRADE
   has a reason_code; every outage drill (feed, model, restart) ends fail closed and reconciles.
4. Hypothetical fills vs later quotes within the backtest slippage assumption, or the assumption is revised.
5. Signal-to-proposal latency measured and a p95 target agreed.

Gate: **[owner]** approval for SHADOW (new approval entries, mode permitted).

## P5: Key separation and risk service (ADR 0003)

Deliverables: `ApprovedOrder` v2 (Ed25519, TTL, environment, account, epoch); `OrderVerifier`;
idempotency store; risk service process with mTLS; kill-switch layers; key rotation.

Acceptance tests: the nine in ADR 0003, plus: planner/orchestrator cannot reach signing or broker
modules; option B (risk service has its own read-only data) passes the oracle property test with a
lying orchestrator.

Decisions made (ADR 0003): self-built vault now (KMS/HSM before CANARY_LIVE), risk service reads its
own data, order TTL 5 s, skew 1 s.

Gate: **[owner]** initialise the vault with your own passphrase (`scripts/vault.py init`, never in
chat); approval entries for the new /risk, /execution and vault code; certificates.

## P6: PAPER with Alpaca Paper

Deliverables: Alpaca Paper `ExecutionPort` adapter (execution service only); `client_order_id`
derivation; broker reconciliation (positions, orders, cash) before every signature and on restart;
PAPER entry in the registry and permitted modes.

Acceptance tests:
1. Adapter contract tests against recorded broker responses: reject, partial fill, cancel, timeout,
   duplicate `client_order_id`, 429, 5xx.
2. A paper-signed order is refused by a service configured for another environment/account.
3. Crash between idempotency write and send never double-sends; recovery resolves by broker lookup.
4. 20 PAPER sessions with drills (3 restarts, feed outage, model outage, broker outage, kill-switch
   test): zero unexplained HALTs, zero unreconciled positions at session end, every order traceable
   to a packet and an approval.
5. Measured slippage, fill rate and latency within the agreed limits.

Gate: **[owner]** Alpaca Paper keys into the secrets manager (scoped to the paper account), approval
for PAPER.

## P7: Observability and durable state

Deliverables: PostgreSQL-class store for packets, orders, idempotency and approvals; durable event
queue; immutable archive (object lock or equivalent); OpenTelemetry metrics/logs/traces; separate
financial/risk dashboard (P&L, exposure, drawdown, denials, reconciliation state).

Acceptance tests: archive records cannot be modified or deleted by the application role; DB
restore reproduces the hash chain; trace links proposal -> risk decision -> order -> fill; alert
fires on each fail-closed trip, on reconciliation mismatch and on kill switch; dashboard figures
equal the report computed from DecisionPackets.

## P8: Active/passive failover

Deliverables: leader election with fencing epoch; passive standby; reconciliation-before-send on
takeover.

Acceptance tests: kill the leader mid-session -> standby takes over, reconciles, resumes in
REDUCE_ONLY, no duplicate or lost order; a paused (not dead) old leader that resumes is refused by
epoch; split-brain simulation never produces two accepted orders for one `order_id`.

## P9: CANARY_LIVE then LIMITED_LIVE

Prerequisites: P1-P8 accepted; live-broker decision (Alpaca Live US-only vs IBKR) made **[owner]**;
live credentials issued by the owner into the secrets manager, never visible to Claude; legal and
account setup by the owner.

Acceptance (CANARY_LIVE): fixed tiny risk budget set in approved config; hard loss limit; kill
switch drill at the live broker; a defined number of sessions with every PAPER criterion repeated;
daily reconciliation against broker statements. LIMITED_LIVE only after CANARY results are reviewed
by the owner and limits are raised through a new risk policy version and approval.

FULL_CONFIGURED_LIVE is not on this roadmap.

## Standing regression suite (runs in every phase)

All existing tests, `lint-imports`, schema drift, risk-config lock, evals runner, invariant oracle,
secret scan on the commit (pre-push gate), and the Claude-cannot-bypass property tests.
