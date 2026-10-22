# Failure-mode register (stage: BACKTEST)

Policy: on ambiguity, stale data, model failure, schema failure, data-feed divergence, broker
reconciliation mismatch, or unknown market state — **FAIL CLOSED for new risk** (CLAUDE.md).

Escalation is implemented in `src/trading_agent/orchestration/fail_closed.py` and
`orchestration/tick.py`. Keep this table and that code in sync; each row names its test.

TradingState: `NORMAL` → `REDUCE_ONLY` (only risk-reducing orders) → `HALTED` (no orders).
Escalate-only within a run; de-escalation requires a new run after human review.

| ID | Failure | Detection | Fail-closed response | State | Audit event | Test |
|---|---|---|---|---|---|---|
| F01 | Stale market/account snapshot or quote | `freshness` rule (`max_snapshot_age_seconds`) | DENY the order | unchanged | DECISION (STALE_DATA / STALE_QUOTE) | AT-10, eval P02 |
| F02 | Missing primary quote for a symbol | Normalizer | Symbol dropped from snapshot ⇒ any order on it DENY (NO_QUOTE) | unchanged | DATA_FAULT | AT-14 |
| F03 | Zero/negative/crossed quote | Normalizer `validate_tick` | Symbol dropped | unchanged | DATA_FAULT | AT-14 |
| F04 | Primary vs secondary divergence > threshold | Normalizer | Symbol dropped | REDUCE_ONLY | DATA_FAULT + STATE_CHANGE | AT-14 |
| F05 | Clock skew (data from the future) | Normalizer / tick | Skip decision | HALTED | DATA_FAULT + STATE_CHANGE | AT-14 |
| F06 | Venue CLOSED/HALTED/UNKNOWN, outside session, instrument halted/restricted/status unknown | `market_state`, `session`, `instrument_status` | DENY | unchanged | DECISION | AT-10, evals P03/P04, `test_each_control_denies` |
| F07 | LLM timeout / transport error / no recording | Planner | NO_TRADE | unchanged | SCHEMA_FAILURE (LLM_UNAVAILABLE) | AT-14 |
| F08 | LLM output not a single valid JSON object / schema-invalid | `parse_strict` | NO_TRADE | unchanged | SCHEMA_FAILURE | AT-06, evals P07/P08/P10/P11 |
| F09 | Hallucinated/unresolvable evidence | `evidence` rule | DENY | unchanged | DECISION | AT-07, eval P09 |
| F10 | Critic unavailable or invalid output | Critic | Treated as critic DENY ⇒ DENY | unchanged | DECISION (CRITIC_DENY) | AT-11, eval C02 |
| F11 | Risk rule or cap raises | Gateway wrapper | DENY (RULE_ERROR) | unchanged | DECISION | AT-09 |
| F12 | Forged/tampered order at execution | Signature check | Order refused | HALTED | STATE_CHANGE | AT-12 |
| F13 | Duplicate order_id at execution | Execution seen-set | Order refused | HALTED | STATE_CHANGE | AT-12 |
| F14 | Broker reject | Ack status | No fill; after 3 consecutive | REDUCE_ONLY | EXECUTION + STATE_CHANGE | AT-14 |
| F15 | Partial fill | Fill vs approved notional | Book the actual fill; reconcile | unchanged | EXECUTION (PARTIAL) | AT-14 |
| F16 | Broker/internal position mismatch | Reconciler after each order | Stop trading | HALTED | RECONCILIATION + STATE_CHANGE | AT-18 |
| F17 | Unknown broker exception | tick `_submit` | Treat broker state as unknown | HALTED | STATE_CHANGE | AT-14 |
| F18 | Audit write failure | `AuditLedger.append` raises | No submission (write-ahead) | HALTED | (cannot be written) metric `audit_write_failure` | AT-14 |
| F19 | Invalid runtime config / non-permitted mode | `load_runtime_config` | Process refuses to start | — | — | AT-03 |
| F20 | Invalid risk policy / universe file | `load_risk_policy` | Process refuses to start | — | — | unit tests |
| F21 | Daily loss limit reached | `loss_limit` rule | DENY new risk; exits allowed | unchanged | DECISION | AT-13 |
| F22 | Unknown data-issue code | `escalation_for` default | — | HALTED | STATE_CHANGE | unit tests |

### Read-only data adapters (`market_data/streaming.py`, `account_data/rest_adapter.py`)

Adapter issues do not escalate TradingState by themselves: the affected quote or account never
reaches a snapshot, so the gateway's NO_QUOTE / freshness rules deny any order that needs it.

| ID | Failure | Detection | Fail-closed response | Test |
|---|---|---|---|---|
| F23 | Malformed message (bad JSON/UTF-8, schema, dup keys, NaN, oversize, naive ts) | `strict_json_object` + wire models | Drop message; reconnect after 20 in a row | `test_md_decoder.py`, `test_md_streaming.py` |
| F24 | Unknown vendor symbol | `InstrumentRegistry.normalize` | Drop message / reject whole account payload | `test_md_instruments.py`, `test_account_adapter.py` |
| F25 | Delayed message (latency > `max_latency`) | `QuoteBook.apply` | Reject; never used | `test_delayed_message_is_rejected_not_used` |
| F26 | Future-dated vendor timestamp | `QuoteBook.apply`, account adapter | Reject | `test_future_dated_*` |
| F27 | Duplicate / reordered message | `SequenceTracker`, `QuoteBook` | Drop | `test_duplicate_and_reordered_messages_dropped` |
| F28 | Sequence gap | `SequenceTracker` | Quarantine feed (or instrument) until snapshot; request resync | `test_*sequence_gap*`, `test_instrument_scope_gap*` |
| F29 | Quote older than `max_age` | `QuoteBook.fresh` | Excluded from snapshots | `test_quotes_go_stale` |
| F30 | Heartbeat silence > timeout | adapter `poll` | Drop connection, clear book, back off | `test_heartbeat_timeout_*` |
| F31 | Disconnect / connect failure | `TransportError` | Clear book; reconnect with exponential backoff + jitter | `test_disconnect_clears_book_*` |
| F32 | Rate limited (vendor message, HTTP 429, client bucket) | `RateLimited` | Wait ≥ Retry-After and ≥ backoff; no requests meanwhile | `test_rate_limit_*`, `test_http_429_*`, `test_client_side_rate_limit` |
| F33 | Credential missing / rejected | `SecretUnavailable` / `AuthRejected` | No connection; back off | `test_auth_rejected_and_unavailable_back_off` |
| F34 | Account payload stale, older version, or partly invalid | account adapter | Return no account (never partial) | `test_account_adapter.py` |
| F35 | Upstream 5xx / timeout / vendor error message | `TransportError` | Back off; growing delay | `test_transport_failures_back_off`, `test_backoff_grows_*` |

### Risk gateway v2 (see docs/risk-gateway.md)

| ID | Failure | Detection | Fail-closed response | Test |
|---|---|---|---|---|
| F36 | Broker view missing, stale, or disagrees with internal book | `broker_consistency` | DENY all orders (exits too) | `test_each_control_denies[BROKER_*]` |
| F37 | Held position cannot be marked (no quote) | exposure caps, `drawdown` | DENY new risk | `test_each_control_denies[UNMARKABLE_POSITION]` |
| F38 | Equity high-water unknown | `drawdown` | DENY new risk | `test_each_control_denies[DRAWDOWN_UNKNOWN]` |
| F39 | Released risk config edited / unpinned / mis-named | `load_risk_policy` | Refuse to start | `test_risk_config.py` |
| F40 | Runtime expects a different policy version | `BacktestEngine` | Refuse to start | `test_runtime_and_policy_versions_must_agree` |
| F41 | Cap returns NaN / non-Decimal | gateway | DENY (RULE_ERROR) | `test_cap_returning_nan_denies` |

### Backtesting on datasets (see docs/backtesting.md)

| ID | Failure | Detection | Response | Test |
|---|---|---|---|---|
| F42 | Dataset file edited after its manifest | `load_dataset` hash check | Refuse to run | `test_dataset_hashes_and_unknown_versions` |
| F43 | Quotes outside listing period (e.g. after delisting) | `load_dataset` | Refuse to run | `test_quotes_after_delisting_are_rejected` |
| F44 | Quotes outside the market calendar | `calendar_violations` | Refuse to run | `test_calendar_rules` |
| F45 | Latency >= bar spacing (fill visible before it happened) | `BacktestEngine.run` | Refuse to run | `test_latency_must_be_below_bar_spacing` |
| F46 | Placeholder dataset id | `run_dataset.main` | Exit 2 | `test_placeholder_dataset_id_is_refused` |

### Live run loop (see docs/paper-trading-plan.md)

| ID | Failure | Detection | Response | Test |
|---|---|---|---|---|
| F47 | Feed outage > ops max | no usable quotes in snapshot | REDUCE_ONLY | `test_feed_outage_goes_reduce_only` |
| F48 | Consecutive model errors >= ops max | planner/critic failures | REDUCE_ONLY | `test_consecutive_model_errors_go_reduce_only` |
| F49 | DecisionPacket write fails | ChainedLog.append | HALTED, loop stops | `test_packet_write_failure_halts` |
| F50 | Clock goes backwards | live loop | HALTED | `test_clock_going_backwards_halts` |
| F51 | Unexpected exception in a cycle | live loop | HALTED, recorded, loop stops | `test_unexpected_exception_halts_and_is_recorded` |
| F52 | Restart inside a session | recovery | >= REDUCE_ONLY | `test_restart_in_session_restores_book_and_fails_closed` |
| F53 | Torn ledger line / book vs broker mismatch at restart | ledger resume + reconcile | line set aside; mismatch => HALTED | `test_torn_execution_record_is_detected_and_halts` |
| F54 | Corrupted ledger chain | ledger resume | refuse to start | `test_corrupted_ledger_refuses_to_start` |
| F55 | HALT at end of session | recovery | stays HALTED until acknowledged | `test_halt_survives_into_the_next_session_until_acknowledged` |

### Pre-evaluation gate (see docs/operating-rules.md)

| ID | Failure | Detection | Response | Test |
|---|---|---|---|---|
| F56 | Strategy or risk version not approved / edited after approval | gate rule 0 (hash-bound approvals) | NO_TRADE, model not called | `test_unapproved_strategy_is_never_operated`, `test_edited_strategy_file_loses_its_approval` |
| F57 | Any prerequisite UNKNOWN (data, account, reconciliation, kill switch, venue, instrument status, features) | gate rules 1-5 | NO_TRADE + reason_code, model not called | `test_each_prerequisite_yields_no_trade_with_reason`, `test_any_unknown_prerequisite_means_no_trade_and_no_model_call` |
| F58 | Kill switch set by operator | gate rule 3 | NO_TRADE and HALTED | `test_kill_switch_halts_the_pipeline` |
| F59 | Model proposes something no trigger selected | post-model check | NO_TRADE PROPOSAL_NOT_TRIGGERED | `test_model_sees_triggers_and_may_only_act_on_them` |
