# Session handoff (state as of the APR-0008 commit, after 66d25f8)

Paste this into a new session, or tell it: "read docs/session-handoff.md, CLAUDE.md and docs/roadmap.md first".

## 1. Ground rules (do not break)

* `CLAUDE.md` is mandatory policy. Claude may **never** change the active mode; credentials, `/risk`,
  `/execution`, live deployment and broker permissions need tests **plus an explicit owner approval**.
* Mode is **BACKTEST** (only mode the code permits: `PERMITTED_MODES_THIS_STAGE`). No live broker code, no
  network code (`tests/architecture/test_no_network_code.py`), no execution credentials anywhere.
* Approvals live in `config/approvals.yaml`, append-only, bound to file SHA-256, BACKTEST only:
  APR-0001 risk-policy.v2 + restricted.v1 · APR-0002 simulator + execution-assumptions.v1 ·
  APR-0003 strategy mean-reversion-demo.v1 + trigger code · APR-0004 in-house vault ·
  APR-0005 ApprovedOrder v2 (Ed25519) + OrderVerifier + simulator change (supersedes APR-0002 for the simulator) ·
  APR-0006 durable idempotency store + client_order_id + simulator/port changes (supersedes APR-0005 for the simulator) ·
  APR-0007 model client + `evaluation_time` in the planner payload + eval set planner-eval.v2 (offline only) ·
  APR-0008 Anthropic Messages transport (key via SecretsProvider), thinking-block tolerance, one-module network allow-list,
  `scripts/run_model_eval.py` (supersedes APR-0007 for client.py/transport.py). Editing an approved
  file voids its approval until a new entry is added.
* Working pattern the owner expects: do the work, run tests, show results, **ask before committing**
  (commits auto-push to a **public** GitHub repo), ask for approvals with an explicit question. Never ask for
  or handle passwords/keys in chat. The owner is new to trading, writes Bulgarian and English; answers
  questions with "you decide" when they lack expertise: then decide and state the reasoning.

## 2. Where things are

* Project: `D:\Projects\Trading\trading-agent` (git, branch `main`, remote `https://github.com/aiskrev/Trading`,
  public). Local git identity `aiskrev <aiskrev@abv.bg>`. Commit trailer: as given by the session's attribution reminder.
  A leftover duplicate at `C:\Users\aiskr\OneDrive\Documents\Projects\Trading` could not be deleted by Claude
  (permission denied): the owner should delete it.
* Latest commits: (APR-0008) Anthropic transport / eval script · 298d69a model client / eval v2 · a7957d5 idempotency · 5ecb906 Ed25519 order signing · 66d25f8 vault · 0ea3fde ADRs/roadmap · d4fc753 evaluation gate · 6a52cfe PAPER readiness ·
  e943c77 dataset backtest · 7bb4c4f push-gate fix · a0a7fd4 planner eval · 41321c3 auto-push.
  Working tree clean; local == GitHub.
* Environment: the project targets **Python 3.12** (`requires-python >=3.12`, CI on 3.12). Dependencies are hash-locked in
  `requirements/{runtime,model,dev}.lock` (+ `sbom.cdx.json`); install with `pip install --require-hashes -r
  requirements/dev.lock` then `pip install --no-deps -e .` (see `requirements/README.md`). Verified: the full suite passes
  on Python 3.12.15 / Linux from a fresh hash-locked venv. **WSL has two distros:** `Ubuntu-22.04` (the owner's real
  vault in `~/.trading-agent`, clone `~/trading-agent`, venv `~/venvs/trading` currently Python **3.10** - recreate it
  with `python3.12 -m venv`, which that distro has) and `Ubuntu` (24.04, system Python 3.12.3 but no `python3-venv` yet).
  **Windows still has only Python 3.9 32-bit**, so the auto-push gate (`.githooks/pre-push`, prefers `py -3.12`) runs under
  3.9 with a printed warning and the git-ignored `.deps/` (old unlocked pydantic 2.13.5 etc.). After installing Windows
  Python 3.12, rebuild it: `py -3.12 -m pip install --require-hashes --target .deps -r requirements/dev.lock`.
  Windows 3.9 command: `PYTHONPATH="src;.deps" py -m pytest -p no:cacheprovider -o addopts="" -q`
  (**567 pass**, ~75 s). In a WSL copy of the tree without `.git`, `test_cli_flow_and_refusals` blocks on a password
  prompt (it relies on the `.git` directory to refuse a vault path inside the repo): `git init` the copy.
  **pydantic is pinned to 2.13.x**: 2.14 changes the generated JSON Schemas (drops Decimal string patterns) and trips
  the schema-drift test; upgrade it deliberately and regenerate `schemas/`.

## 3. Enforcement already in place

* Hooks (`git config core.hooksPath .githooks`): `post-commit` auto-pushes; `pre-push` runs
  `scripts/pre_push_check.py` (secret scan of pushed commits, risk-config lock, full tests in a clean worktree).
  Skip one push: `TRADING_AGENT_NO_AUTOPUSH=1`. Never `--no-verify`.
* `lint-imports` (7 contracts): planner can't reach execution/risk/signing/secrets; only composition roots may
  import `secret_store.vault`; data adapters read-only; strategy triggers deterministic.
* Schema drift (`scripts/export_schemas.py --check`), risk-config lock (`scripts/lock_risk_config.py --check`),
  evals (`python -m evals.runner`, 19/19).
* Gotchas: files written by Python on Windows come out CRLF: normalise with
  `for f in $(git ls-files --eol | awk '$2=="w/crlf"{print $NF}'); do sed -i 's/\r$//' "$f"; done`
  (hash-pinned configs and schemas depend on LF). Bash heredocs with nested quotes break: write helper scripts
  with the Write tool. `Path.write_text(newline=)` doesn't exist on 3.9: use `write_bytes`.

## 4. What exists (all BACKTEST, tested)

* **Pipeline** (`src/trading_agent/`): market_data (streaming + CSV adapters, backoff/jitter, sequence gaps, quote
  book, instrument registry), account_data (read-only REST), normalization, features, planning (planner/critic over
  `LLMClient`, strict parser, scripted/recorded LLMs; payload carries `evaluation_time`), model_client
  (`SdkLLMClient` over a tools-free `ModelTransport`, fake-SDK tested; `AnthropicMessagesTransport` on the Messages API, **not yet run against the real API**), strategy (versioned definition +
  deterministic triggers), risk (gateway, 20+ rules, hash-pinned versioned policy, signs `ApprovedOrder` v2 with Ed25519
  via the `Signer` port), execution (`OrderVerifier` with public keys only, refusals by reason code; `IdempotencyStore`: order_id recorded
  before send, `client_order_id` derivation, restart settlement by broker lookup; `SimulatedBroker`:
  latency, partial fills, fees, corporate-action book events), reconciliation, audit (hash-chained ledger,
  resumable chain, post-trade auditor), backtest (datasets with splits/delistings/calendar, gross/net, 2x-cost
  sensitivity), orchestration (pre-evaluation gate, live loop with DecisionPackets, session metrics, restart
  recovery, fail-closed ops policy), secret_store (SecretsProvider/Signer ports, **in-house vault**).
* **Pre-evaluation gate** (`orchestration/evaluation_gate.py`, `docs/operating-rules.md`): approved versions,
  freshness, reconciliation, mode + kill switch (`ops/KILL_SWITCH`), universe, deterministic trigger; any UNKNOWN =>
  NO_TRADE with `reason_code`, model not called.
* **Vault** (`docs/vault.md`): AES-256-GCM/scrypt store, Ed25519 sign-only keys, per-client ACL, hash-chained access
  log, rollback detection, CLI `scripts/vault.py` (refuses paths inside a git repo). **The owner created the real vault in WSL Ubuntu** (`~/.trading-agent/`, Python 3.12 venv `~/venvs/trading`, repo clone `~/trading-agent`); the Anthropic key and a `model-client` grant are in it (token is the owner's, never in chat).
* Datasets: only the synthetic `synthetic-ca-2026jan.v1` (symbols AAA, BBB). Universe/policy cover only AAA/BBB.
* Planner eval (`docs/evals/`): frozen 255-scenario set `planner-eval.v2` (v1 + clock; v1 files kept), dev/validation/holdout (holdout sealed, never accessed),
  taxonomy graders calibrated with reference models. **`trading_planner.v1` has not been scored** (no model outputs
  exist yet; the transport and `scripts/run_model_eval.py` are built, the first real call has not been made). Candidate `prompts/candidates/trading_planner.v2-candidate.md` is offline only.
* Docs: `architecture.md`, `threat-model.md` (T1-T29), `failure-mode-register.md` (F01-F62), `risk-gateway.md`,
  `backtesting.md`, `paper-trading-plan.md` (partly superseded), `roadmap.md` (P0-P9), ADR 0001-0003.

## 5. Decisions made

* Target architecture (ADR 0002, status **Proposed**, owner has not formally accepted): Sonnet 5.5 via Agent SDK
  with no tools; data Massive MCP (dev only) then Massive WebSocket or Databento; Alpaca Paper then Alpaca Live (US)
  or IBKR; independent risk service; Postgres-class store + queue + immutable archive; OpenTelemetry; active/passive
  with fencing; promotion BACKTEST -> REPLAY -> SHADOW -> PAPER -> CANARY_LIVE -> LIMITED_LIVE.
* ADR 0003 owner answers: no KMS, so **build the vault**; **risk service reads its own market and account data**;
  order TTL **5 s** (max 10), clock skew **1 s** (500 ms drift alarm). Move to managed KMS/HSM before CANARY_LIVE.

## 6. Open items

Owner decisions: passphrase custody/backup; which host runs the vault and risk service; formally accept ADR 0002/0003;
apply the proposed CLAUDE.md amendment ("no promotion into a mode that sends orders to a broker unless ..." so SHADOW
can precede PAPER); data vendor choice and licence; number of PAPER sessions (recommended 20, with drills); Alpaca Live
vs IBKR (deferred); whether/when to approve PAPER.

Owner-only actions: create vendor/broker accounts and put read-only/paper keys into the vault; change the mode; delete
the OneDrive duplicate (sessions were again opened from it: open them from `D:\Projects\Trading	rading-agent`).
Done by the owner: vault created; GitHub password changed and 2FA on. Never paste tokens/keys/passphrases in chat (a
vault client token was pasted once and was revoked and re-issued).

Engineering next steps (each needs tests + an approval entry where it touches /risk, /execution or credentials):
1. ~~ADR 0003 step 1~~ **done (APR-0005)**: `ApprovedOrder` v2, `OrderVerifier`, HMAC removed.
   ~~Step 2~~ **done (APR-0006)**: durable idempotency store, `client_order_id`, restart settlement (live loop
   HALTs on `UNRESOLVED_ORDER`). Next is step 3: risk service as its own process behind a narrow
   request/response schema with mTLS (also read approved policy hashes from approvals; needs the host decision).
   Editing `simulated_broker.py`/`idempotency.py`/`ports.py` voids APR-0006 for them.
2. ~~`SimulatedBroker.restore_positions`~~ done in APR-0006.
3. Wire `VaultSecretsProvider` into adapters; hash-locked dependency file (new `cryptography` dep).
4. ~~Model client, `evaluation_time`, eval set v2, real transport~~ **built (APR-0007/0008)**. Left: the owner's first real
   call (`git pull`, `pip install "anthropic>=1,<2"`, `export TRADING_VAULT_TOKEN=...`, then
   `python scripts/run_model_eval.py trading_planner.v1 --split development --limit 3`), then `--all` for
   `trading_planner.v1` and `trading_planner.v2-candidate` on development and validation; choose a prompt from those
   results (holdout once, last, per `docs/evals/planner-evaluation.md`); the choice needs an approval. Also tune
   `effort`/`max_tokens` from latency and cost, and hash-lock the `anthropic` dependency before PAPER.
5. Real dataset in the `data/datasets/` format + new risk policy version/approval for real symbols.
6. Risk service as a separate process (mTLS), then durable store/OTel/failover per `docs/roadmap.md`.
