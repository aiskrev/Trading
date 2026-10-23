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
| T4 | Forged or tampered order bypassing the gateway | TB3 | A1 | Ed25519-signed `ApprovedOrder` v2 bound to environment/account/epoch/policy/TTL; execution holds public keys only (`OrderVerifier`); planner cannot import signer | AT-01, AT-12, `tests/invariants/test_order_signing.py` |
| T5 | Order replay / duplicate submission | TB3 | A1 | `order_id` recorded durably (hash-chained, fsynced) before processing; reuse ⇒ DuplicateOrder ⇒ HALTED, also after a restart; 5 s order lifetime; `client_order_id` derived from the order; duplicate-symbol open-order rule | AT-12, `tests/invariants/test_idempotency.py` |
| T6 | Critic raising size, flipping side or overriding DENY | TB2 | A1 | `CriticBody` has no side/symbol fields; REDUCE is an upper bound; critic DENY is a gate failure; critic failure ⇒ DENY | AT-11, evals C01–C05 |
| T7 | Mode escalation (BACKTEST → live) | TB5 | A4 | Frozen `RuntimeConfig`; no setter; startup refuses non-BACKTEST; registry has only `SimulatedBroker`; CODEOWNERS on mode/registry | AT-02, AT-03 |
| T8 | Credential leakage (prompts, logs, tests, git) | TB5 | A2 | No credentials needed or present; `SecretsProvider` stub raises; log redaction; repo secret scan + gitleaks in CI/pre-commit; `.gitignore` for key files | AT-04 |
| T9 | Unapproved change to risk limits / prompts | TB5 | A3, A6 | CODEOWNERS + 2 approvals; released risk config hash-pinned and refused if edited; policy SHA-256 on every decision; prompt SHA-256 per decision | AT-19, `test_risk_config.py` |
| T10 | Data-feed poisoning or divergence | TB1 | A1 | Cross-source divergence check ⇒ symbol dropped + REDUCE_ONLY; crossed/zero prices dropped | AT-14 |
| T11 | Stale data / clock skew | TB1 | A1 | Freshness rule on snapshots and quotes; future-dated data ⇒ HALTED | AT-10, AT-14 |
| T12 | Audit tampering or loss | — | A5 | Hash-chained records; write-ahead DECISION record; audit failure ⇒ HALTED | AT-19, AT-14 |
| T13 | Martingale / revenge trading after losses | TB2 | A1 | Static limits; daily loss limit blocks new risk; no loss-scaled sizing exists | AT-13 |
| T14 | Dependency / supply-chain compromise | TB5 | all | Few deps; every package pinned to an exact version with SHA-256 hashes (`requirements/*.lock`, installed with `--require-hashes` in CI); SBOM generated from the lock; pydantic pinned to the minor that generates the committed schemas. Open: CI actions pinned to tags, not SHAs; `pip-audit` | `tests/security/test_dependency_lock.py` |
| T15 | Rule bug throws ⇒ order slips through | — | A1 | Every rule/cap wrapped; exception ⇒ RULE_ERROR ⇒ DENY | AT-09 |

### Read-only data adapters

| ID | Threat | Boundary | Assets | Controls | Test |
|---|---|---|---|---|---|
| T16 | Adapter used to send orders or write account state | TB4 | A1 | `HttpTransport` and `ReadOnlyHttpClient` expose only `get`; path allow-list; stream transport has no order verbs; import-linter forbids adapters importing execution/risk/planning/signing; credential must be a read-only-scoped token | `test_client_is_get_only_and_allow_listed`, AT-01 |
| T17 | Credential leak via adapter logs, errors, health or records | TB4 | A2 | Token fetched per connect from `SecretsProvider`, passed only to the transport, reference dropped; issue details never echo payloads; redacting logger; account number never stored (alias only) | `test_adapter_secrets.py` |
| T18 | Hostile/garbled vendor payloads (oversize, NaN, dup keys, unknown symbols) | TB1 | A1 | Size cap, strict JSON, `extra="forbid"` wire models, registry-only symbols, all-or-nothing account decode | `test_md_decoder.py`, `test_account_adapter.py` |
| T19 | Replayed / delayed / reordered data presented as current | TB1 | A1 | Latency cap, future-timestamp cap, sequence tracking, monotonic account version, max-age freshness | `test_md_streaming.py` |
| T20 | Reconnect storm / rate-limit ban | TB4 | availability | Equal-jitter exponential backoff, Retry-After honoured, client token bucket | `test_md_primitives.py`, `test_rate_limit_*` |
| T21 | Premature real network code | TB5 | A1, A2 | No module may import socket/ssl/http/urllib/requests/websockets/anthropic… except the one allow-listed model transport (`model_client/anthropic_transport.py`: key only via `SecretsProvider`, no tools, no `base_url`); execution stays `SimulatedBroker` only | `test_no_network_code.py`, `tests/model_client/` |

## Open items before PAPER

1. ~~Hash-locked dependency lockfile and SBOM (T14).~~ Done: `requirements/*.lock`, `requirements/sbom.cdx.json`. Remaining: SHA-pin the CI actions; add `pip-audit`.
2. ~~Real `SecretsProvider` against the approved secrets manager~~ Done for the vault (`VaultSecretsProvider`, used by `scripts/run_model_eval.py`). To do: wire it into the data and broker adapters, with tests proving values never reach logs/prompts (T8).
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

### Control room (`ui/`, see ui/README.md)

| ID | Threat | Controls | Test |
|---|---|---|---|
| T30 | A web page in the owner's browser (or another machine) drives the local UI: cross-site request, DNS rebinding | listens on 127.0.0.1 only; `Host` must be `127.0.0.1`/`localhost` with the server's port; `Origin` checked on POST; per-start token in `X-UI-Token`; no CORS headers, no cookies | `test_it_listens_on_loopback_only`, `test_a_wrong_host_header_is_refused`, `test_a_post_needs_the_token_the_origin_and_a_small_json_body` |
| T31 | The UI is used to change the mode, a config, an approval, a prompt, the kill switch or a secret, to place an order or to call the model | no such route; the only POST starts one of eight fixed jobs (argv built server-side); no import of execution, risk, signing, vault, model-client or feed code; files written only under `runs/ui/` | `test_the_served_code_imports_no_order_risk_vault_signing_model_or_feed_code`, `test_files_are_written_only_by_the_job_runner_and_only_under_runs_ui`, `test_nothing_in_the_ui_can_start_the_vault_the_model_or_change_a_mode` |
| T32 | Command injection through a job name or dataset id | job key from a fixed table; dataset id must match a safe pattern AND exist under `data/datasets/`; no shell | `test_a_job_can_only_be_one_of_the_fixed_keys_and_a_dataset_that_exists` |
| T33 | Path traversal through run names, document paths, report ids or static files | names matched against a safe pattern and resolved under a fixed base; documents only from an allow-listed index; static files from a whitelist | `test_names_from_the_browser_cannot_escape_their_folders`, `test_static_files_are_a_whitelist_and_cannot_be_traversed` |
| T34 | Script injection (XSS) through data shown on the page: news text, model output, file contents | data inserted as text nodes only; no `innerHTML`/`eval`; strict Content-Security-Policy (`script-src 'self'`, no inline script or external asset) | `test_the_front_end_has_no_html_injection_eval_or_external_assets`, `test_every_response_carries_the_security_headers` |
| T35 | A secret reaches the page, a job's output or a child process | the UI never reads the vault or the model key; child processes get an environment without token/key/secret/password variables; job output is redacted; errors carry no paths or stack traces | `test_job_processes_never_inherit_secrets_from_the_environment`, `test_job_output_is_redacted`, `test_errors_do_not_leak_paths_or_stack_traces` |
| T36 | Verification changes evidence (moving a torn line aside, rewriting a chain) | chain checks are read-only re-implementations; tests compare bytes and mtimes before and after | `test_chain_checks_catch_tampering_and_never_modify_a_file`, `test_verifying_the_real_demo_run_does_not_change_it` |
| T37 | The sealed planner-eval holdout is read through the UI | scenario routes accept only development and validation | `test_the_sealed_holdout_is_never_exposed` |
