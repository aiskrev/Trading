# ADR 0003: Separating the order-signing key from the order-sending service

Status: **Design accepted by the owner on the three open questions (see Decisions); implementation in
progress.** Implementing it touches /risk, /execution and credentials, so every step needs tests plus an
explicit approval entry.

## Problem

ADR 0001 made `RiskGateway` the only component that can mint an `ApprovedOrder`, signed with an
HMAC key generated in-process and shared with the execution port. That is sufficient in one
process. In the target architecture the gateway and the execution adapter are different services:

* A **shared** secret (HMAC) means whoever can *verify* an order can also *forge* one. The execution
  service is the component holding broker credentials, so it must not also be able to sign.
* An **ephemeral in-memory** key cannot be shared across processes or survive failover.
* Nothing binds an approved order to an environment, time, account, or leader, so a signed paper order
  could be replayed after a restart, against another account, or from a stale leader.

## Goals

1. Only the risk service can create orders the execution service accepts.
2. Compromise of the execution service, orchestrator, planner or Claude cannot produce a valid order.
3. A signed order is valid only for one environment, one account, a short time window, once.
4. Keys can rotate and be revoked without downtime or editing code.
5. The kill switch works even if the orchestrator is compromised.
6. Private keys never appear in source, prompts, logs, tests, screenshots or git (CLAUDE.md).

## Design

### Roles and keys

| component | holds | can |
|---|---|---|
| **risk service** | private signing key (non-exportable; sign-only access to KMS/HSM or secrets manager; in development an ephemeral key generated at start) | evaluate proposals, sign `ApprovedOrder` |
| **execution service** | public keys only (`key_id -> public key`, validity window, revoked flag) + broker credentials | verify, send, reconcile |
| **orchestrator / planner / Claude** | neither | request evaluation; no route to the broker; cannot read keys |

Algorithm: **Ed25519** (small, deterministic, no parameter choices). This needs the `cryptography`
package, a new dependency to be reviewed and pinned (hash-locked) before use. `hmac` stays for
tests of the old path until removed.

### Signed envelope (`ApprovedOrder` v2)

Signature covers canonical JSON (existing `canonical_json`) of:

```
order_id, decision_id, proposal_id, symbol, side, limit_price, notional,
issued_at, expires_at (issued_at + ttl, ttl <= 10 s, from config),
environment (mode value, e.g. "PAPER"), broker_account_alias,
leader_epoch, policy_version, policy_sha256, market_snapshot_id, account_snapshot_id,
key_id, alg
```

The signature field is excluded from the payload (as today). The signature is never written to the
audit log or DecisionPacket.

### Execution-service verification (all must hold, else refuse and alert)

1. `alg` and `key_id` known; key valid at `issued_at`; key not revoked.
2. Signature valid over the exact payload received.
3. `now` in `[issued_at - skew, expires_at]`.
4. `environment` equals the service's own mode; `broker_account_alias` equals its configured account.
5. `leader_epoch` equals the service's current epoch (older epoch => stale leader).
6. `order_id` unseen: recorded in a durable idempotency table *before* sending; the broker
   `client_order_id` is derived from `order_id`, so retries cannot duplicate.
7. `policy_sha256` is on the execution service's list of approved policy hashes (from approvals).
8. Kill switch off and execution state not HALTED.

### Independence of the risk service

Two options for what the risk service trusts:

* **A. Snapshots supplied by the orchestrator.** Simple; a compromised orchestrator could lie about
  prices or positions.
* **B. Risk service reads market data and account state itself** (read-only credentials), and takes
  only the *proposal* from the orchestrator. Costs a second feed subscription; removes the lie.

**Recommendation: B for PAPER and above, A during SHADOW.** In A the risk service still cross-checks
snapshot hashes, freshness and broker reconciliation. Either way the risk service signs only after
its own reconciliation with the broker (read-only) succeeds.

### Transport and authentication

* Service-to-service mutual TLS; each service has its own certificate. The planner process has no
  network route to the execution service or broker.
* The risk service accepts requests only from the orchestrator identity; it rate-limits and logs
  every request (with proposal and policy hashes, not secrets).

### Rotation and revocation

* Keys are published as `key_id`, public key, `not_before`, `not_after`. Rotation = publish new key,
  switch the risk service to sign with it, retire the old after `ttl` plus a margin; both are
  valid during the overlap.
* Revocation list is read by the execution service on every verification (cached for seconds,
  fail closed if unreadable).
* Key publication and revocation are changes to approved configuration (CODEOWNERS + approval).

### External kill switch (works without trusting the orchestrator)

Three independent layers, any one stops new risk:

1. the risk service stops signing (checks the switch on each request);
2. the execution service refuses to send (checks on each order);
3. an operator action at the broker side: disable the order-capable API key / cancel-all.

The switch lives outside the application (e.g. a flag in the secrets manager or a broker
permission), is set only by a human, and an unreadable state means "active".

## Threat model additions

| threat | control | test |
|---|---|---|
| execution service compromised, forges orders | holds no private key | execution test double cannot sign; no private key in its environment |
| orchestrator/planner compromised | no key, no broker route; risk service independent (option B) | import-linter contracts + network policy test |
| replay of a valid order | `expires_at`, idempotency table, `client_order_id` | replay, delayed and duplicate tests |
| paper order sent to live (or reverse) | `environment`, `broker_account_alias` in signature | cross-environment refusal test |
| stale leader sends after failover | `leader_epoch` check | stale-epoch test |
| stolen/rotated-out key | `not_after`, revocation list | revoked and expired key tests |
| key exfiltration | sign-only KMS/HSM; dev key ephemeral; logs redact | secret-scan and log-redaction tests |
| kill switch unreadable | treated as ACTIVE | unreadable-switch test |

## Acceptance tests (what "done" means)

1. Risk-service signature verifies at the execution service; any single changed field fails.
2. Execution service has only public keys: constructing it with a private key is rejected.
3. Refusals (each its own test): unknown `key_id`, revoked key, expired order, order from the future,
   wrong environment, wrong account, stale epoch, replayed `order_id`, unapproved `policy_sha256`.
4. A crash between "record `order_id`" and "send" never produces a second broker order (same
   `client_order_id`); recovery decides sent/not-sent by broker lookup, else HALT.
5. Rotation: old and new key both verify during overlap; old fails after `not_after`.
6. Kill switch: set => risk service returns no order, execution service refuses a previously signed
   order, both audited; unreadable => same.
7. Planner and orchestrator packages cannot import signing or broker modules (import-linter).
8. No private key material in repo, logs, DecisionPackets or audit records (existing secret scans plus a
   targeted test).
9. Property test: for arbitrary proposals and arbitrary orchestrator-supplied inputs, every order the
   execution service accepts also satisfies the independent oracle (`tests/invariants/risk_oracle.py`).

## Migration steps (each is its own approved change)

1. Introduce `ApprovedOrder` v2 and `OrderVerifier` (public-key only) in-process, with Ed25519 and an
   ephemeral key; keep the current tests green. Replace HMAC. **Implemented** (`core/signing.py`,
   `execution/verifier.py`, `tests/invariants/test_order_signing.py`): verification checks 1-5 and 7;
   acceptance tests 1, 2, 3 and 5. Check 6 is still the simulator's in-memory `order_id` set (step 2);
   check 8 is step 5. In-process the verifier trusts the loaded policy's hash (its approval is checked
   at startup); reading the hash list from `config/approvals.yaml` belongs with the process split
   (step 3). Known limit: a raw 32-byte private seed is indistinguishable from a public key by length,
   so `TrustedKey` rejects signers, key objects and 64-byte encodings, not a mislabelled seed.
2. Add the idempotency store and `client_order_id` derivation to the simulator, still in-process.
3. Split the risk service into its own process behind a narrow request/response schema; mTLS.
4. Move the signing key to the secrets manager / KMS (owner action: provide it; never in chat).
5. Add leader epoch and the external kill switch layers.

## Decisions (owner answers, 2026-10-22)

1. **Secrets and signing keys: no KMS/HSM is available, so the project builds its own vault**
   (`src/trading_agent/secret_store/vault.py`, ADR text below). Owner instruction: "create a secrets
   manager".
2. **The risk service reads its own market and account data (option B)** for PAPER and above. It takes
   only the *proposal* from the orchestrator.
3. **Order lifetime and clock skew: delegated to the engineering recommendation:**
   * `order_ttl_seconds = 5` (hard maximum 10). The signed order only has to survive the hop from the
     risk service to the execution service, including one retry of the same `order_id`. Anything slower
     is stale for a limit order priced at the touch, and a short window shrinks replay exposure.
   * `clock_skew_seconds = 1`, and both services must be NTP-synchronised. If a service measures its clock
     more than 500 ms away from a reference, it refuses to sign/send and alerts (fail closed). Skew
     tolerance extends the accepted window on the early side only; the expiry is never extended.
   * A retry after expiry is not resent: the order goes back through the risk service, which re-checks
     everything and issues a *new* `order_id`.

## Vault (the self-built secrets manager)

What it is: an encrypted, access-controlled store for secrets and **sign-only signing keys**, with
a hash-chained access log. What it is not: an HSM. It runs on hosts the operator controls.

* **At rest:** every entry is AES-256-GCM encrypted under a key derived with scrypt from a passphrase
  that only the operator holds (typed at start; never stored, logged or passed as an argument). Entry
  name and version are authenticated data, so entries cannot be swapped or rolled back undetected.
* **Signing keys** (Ed25519) are generated inside the vault and have **no export operation**. The only
  operations are `sign(key_id, message)` and `public_key(key_id)`. Secrets (broker tokens, API keys)
  are readable by name, only by clients whose ACL allows that name.
* **Clients** are identified by a per-client random token, stored only as a hash. The ACL says which
  secret names a client may read and which signing keys it may use. The planner/orchestrator client has
  none.
* **Access log:** each allow/deny (who, what name, outcome, time; never values) is appended to a
  hash-chained `ChainedLog`. A log write failure denies the access.
* **Honest limits:** it protects against the vault file or backups leaking, secrets in git/logs/
  prompts, an unauthorised client, and tampering. It does **not** protect against an attacker with root
  on the host or a debugger on the unlocked process, and the passphrase is a single point of failure.
  Before CANARY_LIVE the `Signer` and `SecretsProvider` backends should move to a managed KMS/HSM;
  both are interfaces, so that is a configuration change plus an approval, not a rewrite.
* **Process separation** (vault and risk service as separate OS processes, mTLS) remains migration
  step 3; the library is written so the same API can sit behind that boundary.

## Remaining open questions

* Passphrase custody: who holds it, and a recovery procedure (offline backup of the vault file plus a
  sealed passphrase copy), owner to decide.
* Which host(s) run the vault and risk service (needed for the process-separation step).
