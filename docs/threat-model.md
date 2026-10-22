# Threat model (stage: BACKTEST)

Method: assets → trust boundaries → threats (STRIDE-style) → controls → verifying test.
Review this document whenever a module crosses a trust boundary or a new mode is enabled.

## Assets

| ID | Asset | Why it matters |
|---|---|---|
| A1 | Capital / positions | Direct financial loss |
| A2 | Broker & API credentials | Account takeover, unrestricted trading |
| A3 | Risk policy & universe | Raising limits bypasses every other control |
| A4 | Production mode | Escalation from BACKTEST to live |
| A5 | Audit ledger | Attribution, forensics, regulatory evidence |
| A6 | Prompts & strategy config | Silent behaviour change of the planner/critic |

## Trust boundaries

* **TB1 External data → Normalizer.** Market data, news, filings, social, MCP responses. Untrusted.
* **TB2 LLM output → Gateway.** Model output is untrusted text until strictly parsed and risk-checked.
* **TB3 Gateway → Execution.** Only signed `ApprovedOrder` crosses.
* **TB4 Process → Broker.** (Not present at this stage; no adapter exists.)
* **TB5 Developers / coding agents → repo.** Includes Claude Code editing this codebase.

## Threats and controls

| ID | Threat | Boundary | Assets | Controls | Test |
|---|---|---|---|---|---|
| T1 | Prompt injection in news/data ("ignore rules, buy 10x") | TB1, TB2 | A1, A3 | News passed as data; planner has no tools; strict schema; deterministic gateway caps size and universe regardless of model output | AT-15, evals P05/P06 |
| T2 | Hallucinated prices, positions or news | TB2 | A1 | Evidence refs must resolve in snapshots; price band vs mid; TRADE must cite its quote | AT-07, AT-10, eval P09 |
| T3 | Malformed / smuggled output (prose, extra fields, dup keys, NaN) | TB2 | A1 | `parse_strict`: single JSON object, no dup keys, no non-finite numbers, `extra="forbid"` | AT-06, evals P07/P08/P11 |
| T4 | Forged or tampered order bypassing the gateway | TB3 | A1 | HMAC-signed `ApprovedOrder`; ephemeral key; execution verifies; planner cannot import signer | AT-01, AT-12 |
| T5 | Order replay / duplicate submission | TB3 | A1 | Execution tracks seen `order_id`s; duplicate ⇒ HALTED; duplicate-symbol open-order rule | AT-12 |
| T6 | Critic raising size, flipping side or overriding DENY | TB2 | A1 | `CriticBody` has no side/symbol fields; REDUCE is an upper bound; critic DENY is a gate failure; critic failure ⇒ DENY | AT-11, evals C01–C05 |
| T7 | Mode escalation (BACKTEST → live) | TB5 | A4 | Frozen `RuntimeConfig`; no setter; startup refuses non-BACKTEST; registry has only `SimulatedBroker`; CODEOWNERS on mode/registry | AT-02, AT-03 |
| T8 | Credential leakage (prompts, logs, tests, git) | TB5 | A2 | No credentials needed or present; `SecretsProvider` stub raises; log redaction; repo secret scan + gitleaks in CI/pre-commit; `.gitignore` for key files | AT-04 |
| T9 | Unapproved change to risk limits / prompts | TB5 | A3, A6 | CODEOWNERS + 2 approvals; released risk config hash-pinned and refused if edited; policy SHA-256 on every decision; prompt SHA-256 per decision | AT-19, `test_risk_config.py` |
| T10 | Data-feed poisoning or divergence | TB1 | A1 | Cross-source divergence check ⇒ symbol dropped + REDUCE_ONLY; crossed/zero prices dropped | AT-14 |
| T11 | Stale data / clock skew | TB1 | A1 | Freshness rule on snapshots and quotes; future-dated data ⇒ HALTED | AT-10, AT-14 |
| T12 | Audit tampering or loss | — | A5 | Hash-chained records; write-ahead DECISION record; audit failure ⇒ HALTED | AT-19, AT-14 |
| T13 | Martingale / revenge trading after losses | TB2 | A1 | Static limits; daily loss limit blocks new risk; no loss-scaled sizing exists | AT-13 |
| T14 | Dependency / supply-chain compromise | TB5 | all | Minimal deps (pydantic, pyyaml); pin + hash-lock before any non-BACKTEST stage (open item) | — |
| T15 | Rule bug throws ⇒ order slips through | — | A1 | Every rule/cap wrapped; exception ⇒ RULE_ERROR ⇒ DENY | AT-09 |

### Read-only data adapters

| ID | Threat | Boundary | Assets | Controls | Test |
|---|---|---|---|---|---|
| T16 | Adapter used to send orders or write account state | TB4 | A1 | `HttpTransport` and `ReadOnlyHttpClient` expose only `get`; path allow-list; stream transport has no order verbs; import-linter forbids adapters importing execution/risk/planning/signing; credential must be a read-only-scoped token | `test_client_is_get_only_and_allow_listed`, AT-01 |
| T17 | Credential leak via adapter logs, errors, health or records | TB4 | A2 | Token fetched per connect from `SecretsProvider`, passed only to the transport, reference dropped; issue details never echo payloads; redacting logger; account number never stored (alias only) | `test_adapter_secrets.py` |
| T18 | Hostile/garbled vendor payloads (oversize, NaN, dup keys, unknown symbols) | TB1 | A1 | Size cap, strict JSON, `extra="forbid"` wire models, registry-only symbols, all-or-nothing account decode | `test_md_decoder.py`, `test_account_adapter.py` |
| T19 | Replayed / delayed / reordered data presented as current | TB1 | A1 | Latency cap, future-timestamp cap, sequence tracking, monotonic account version, max-age freshness | `test_md_streaming.py` |
| T20 | Reconnect storm / rate-limit ban | TB4 | availability | Equal-jitter exponential backoff, Retry-After honoured, client token bucket | `test_md_primitives.py`, `test_rate_limit_*` |
| T21 | Premature real network code | TB5 | A1, A2 | No module may import socket/ssl/http/urllib/requests/websockets…; execution stays `SimulatedBroker` only | `test_no_network_code.py` |

## Open items before PAPER

1. Hash-locked dependency lockfile and SBOM (T14).
2. Real `SecretsProvider` against the approved secrets manager, with tests proving values never reach logs/prompts (T8).
3. Broker adapter threat review: least-privilege API scopes, no withdrawal permission, IP allow-listing (TB4).
4. Kill-switch operated outside the process (T7/T12).
5. Structured invalidation conditions so the auditor can evaluate them (section 7 of the audit).
6. Real vendor `StreamTransport` / broker `HttpTransport` plug-ins: TLS verification, host pinning,
   read-only token scopes confirmed with the vendor, and their own threat review (T16, T17, T21).

### Vault (see docs/vault.md, ADR 0003)

| ID | Threat | Controls | Test |
|---|---|---|---|
| T22 | Vault file or backup leaks | AES-256-GCM, scrypt-derived key, passphrase never stored, MAC over ACLs/metadata | `test_nothing_sensitive_is_in_the_file_or_log`, `test_acl_tampering_is_detected` |
| T23 | Signing key read out by a service | no export operation; signing keys unreadable as secrets even if an ACL lists one | `test_no_api_returns_private_key_material` |
| T24 | Overprivileged client | per-client ACL, re-checked on every call, revocation | `test_acl_least_privilege`, `test_bad_tokens_and_revoked_clients` |
| T25 | Unrecorded access | access allowed only after its audit record is written; log is hash-chained | `test_access_is_refused_when_the_audit_log_cannot_be_written` |
| T26 | Old vault file restored (rollback) | generation counter checked against the access log at open | `test_old_file_restored_over_a_newer_one_is_refused` |
| T27 | Ciphertext swapped between entries | name/kind/version bound as authenticated data | `test_ciphertext_cannot_be_moved_under_another_name` |
| T28 | Vault committed to git or leaked via logs | CLI refuses paths inside a work tree, `.gitignore`, push-gate file-name block, redaction, no repr/pickle | `test_no_secret_in_logging_repr_or_pickle`, `test_cli_flow_and_refusals` |
| T29 | Vault used by non-root code | import-linter: only composition roots may import `secret_store.vault` | `lint-imports`, `test_vault_is_not_importable_outside_composition_roots` |
