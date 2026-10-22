# Vault (in-house secrets manager)

Code: `src/trading_agent/secret_store/vault.py` · CLI: `scripts/vault.py` · Design and limits: ADR 0003.
Status: **built and tested; not yet an "approved secrets manager" until you approve it** (CLAUDE.md:
credentials changes need tests plus explicit approval). Nothing in the repo uses a real vault yet.

## What it does

* Encrypted store (AES-256-GCM, key from your passphrase via scrypt) for secrets such as broker and
  data-vendor keys.
* Ed25519 **signing keys that are generated inside the vault and can never be read out**: services can
  only call `sign` and `public_key`.
* Per-client tokens with least-privilege ACLs (which secrets a client may read, which keys it may sign
  with). The planner/orchestrator gets no grants.
* Every allow/deny is appended to a hash-chained access log (no values, only ids and message hashes);
  an access is refused if it cannot be logged. Restoring an older vault file is detected.

## Operating it (you, in your own terminal; never in chat)

```bash
python scripts/vault.py init                         # hidden passphrase prompt, twice
python scripts/vault.py put-secret alpaca-paper-key  # value typed at a hidden prompt
python scripts/vault.py create-key risk-signing-1 --days 30
python scripts/vault.py grant risk-service --read alpaca-paper-key --sign risk-signing-1
python scripts/vault.py export-registry              # public keys for the execution service
python scripts/vault.py verify-log
```

* The vault lives in `~/.trading-agent/` by default; the CLI **refuses any directory inside a git work tree**.
  `*.vault*` is git-ignored and blocked by the push gate.
* `grant` prints the client token once. Put it in the service's own environment. It is stored only hashed.
* Back up the `.vault` file; keep the passphrase offline and separate. There is no recovery without it.

## What it does not protect against

Root on the host, a debugger or memory dump of an unlocked process, a stolen passphrase, or a
compromised service that legitimately holds a token (it can use what it was granted, within its ACL, and
every use is logged). The passphrase is a single point of failure. Before CANARY_LIVE move the `Signer`
and `SecretsProvider` backends to a managed KMS/HSM (same interfaces).

## Not done yet (next steps in ADR 0003)

Running the vault/risk service as a separate OS process behind mTLS; `ApprovedOrder` v2 with the
`OrderVerifier`; wiring `VaultSecretsProvider` into the adapters; hash-locked dependency file.
