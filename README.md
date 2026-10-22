# trading-agent

Architectural skeleton for a controlled algorithmic trading system in which Claude *proposes*
and a deterministic risk gateway *decides*. **Stage 1: BACKTEST only.** No live broker code, no broker
credentials, no unrestricted order submission. The only network code is the model transport
(`model_client/anthropic_transport.py`), which reads its key from the vault.

Read first: [`CLAUDE.md`](CLAUDE.md) (mandatory policy) · [architecture](docs/architecture.md) ·
[threat model](docs/threat-model.md) · [failure-mode register](docs/failure-mode-register.md) ·
[ADR 0001](docs/adr/0001-planner-never-executes.md) · [risk gateway](docs/risk-gateway.md) · [planner evaluation](docs/evals/planner-evaluation.md) · [backtesting](docs/backtesting.md) · [PAPER plan](docs/paper-trading-plan.md) · [operating rules](docs/operating-rules.md) · [roadmap](docs/roadmap.md) · [ADR 0002](docs/adr/0002-target-architecture-and-promotion-path.md) · [ADR 0003](docs/adr/0003-risk-service-key-separation.md) · [vault](docs/vault.md) · [model client](docs/model-client.md) · [locked dependencies](requirements/README.md)

## Quick start

Python **3.12**, dependencies installed from hash-locked files (details in [`requirements/README.md`](requirements/README.md)):

```bash
python3.12 -m venv ~/venvs/trading
~/venvs/trading/bin/pip install --require-hashes -r requirements/dev.lock
~/venvs/trading/bin/pip install --no-deps -e .
pytest                                  # unit, invariants, replay, fault injection, security, architecture
lint-imports                            # module boundaries (AT-01)
python scripts/export_schemas.py --check   # schema drift (AT-05)
python -m evals.runner                  # regression evals (AT-20)
python -m trading_agent.backtest.run    # demo backtest on bundled fixtures
```

`pytest` is configured to add `src` to the path. (A Windows checkout may still use the git-ignored `.deps/` directory
with Python 3.9 for the push gate until Python 3.12 is installed there; CI and WSL run 3.12.)

## Auto-push to GitHub

Every commit is pushed to `origin` automatically, but only after a gate passes:

```bash
git config core.hooksPath .githooks     # once per clone
```

* `.githooks/post-commit` pushes the current branch after each commit.
* `.githooks/pre-push` runs `scripts/pre_push_check.py`: secret scan of every commit being pushed
  (added lines and file names), the risk-config lock check, and the full test suite in a clean
  temporary worktree of the exact commit. Any failure aborts the push; the commit stays local.
* Skip the push for one commit: `TRADING_AGENT_NO_AUTOPUSH=1 git commit ...`. Do not use
  `--no-verify`; it bypasses the secret scan.
* Authentication is handled by Git Credential Manager (browser sign-in once). Never put a password
  or token in the remote URL, config, or this repo.
* Pushing straight to `main` skips pull-request review, so CODEOWNERS review does not apply (see the comment at the
  top of `CODEOWNERS`). The enforced gate for risk changes is `config/approvals.yaml` (hash-bound, checked at startup).

## Layout

```
config/     runtime (mode=BACKTEST), risk policy, universe
prompts/    versioned system prompts (hash recorded in every decision)
schemas/    JSON Schemas generated from src/trading_agent/contracts
src/trading_agent/
  core/ contracts/ market_data/ account_data/ normalization/ features/ strategy/ planning/ model_client/ risk/
  execution/ reconciliation/ audit/ backtest/ orchestration/ observability/ secret_store/ testing/
evals/      guardrail regression cases (planner + critic) and the planner-quality eval (planner_quality/)
requirements/  hash-locked dependencies (runtime, model, dev) and the SBOM
scripts/    operator tools: vault.py, run_model_eval.py, lock_risk_config.py, export_schemas.py, make_sbom.py
tests/      architecture/ unit/ invariants/ replay/ fault_injection/ security/ live/ model_client/ evals/ backtest/
```

## Acceptance tests

| ID | Where |
|---|---|
| AT-01 import boundaries | `tests/architecture/test_boundaries.py` |
| AT-02/03 adapters & frozen mode | `tests/architecture/test_mode_and_adapters.py` |
| AT-04 secrets | `tests/security/test_secrets.py` |
| AT-05 schema drift | `tests/unit/test_schemas.py` |
| AT-06/07 planner output & evidence | `tests/unit/test_planner.py` |
| AT-08–13 risk invariants | `tests/invariants/test_risk_invariants.py`, `tests/invariants/test_claude_cannot_bypass.py` |
| Immutable versioned risk config | `tests/unit/test_risk_config.py` |
| AT-14/15 fault injection & injection | `tests/fault_injection/` |
| AT-16/17 replay & backtest | `tests/replay/` |
| AT-18 reconciliation | `tests/fault_injection/test_faults.py` |
| AT-19 audit | `tests/unit/test_audit.py` |
| AT-20 evals gate | `tests/unit/test_evals_gate.py` |
| Planner prompt quality eval (frozen set, sealed holdout) | `tests/evals/test_planner_quality.py` |
| Read-only market data (malformed, delayed, gaps, reconnect, rate limit) | `tests/market_data/` |
| Read-only account data | `tests/account_data/` |
| Adapter secrets never leak | `tests/security/test_adapter_secrets.py` |
| No network code except the model transport; execution mock-only | `tests/architecture/test_no_network_code.py` |
| Order signing, idempotency, restart recovery | `tests/invariants/test_order_signing.py`, `tests/invariants/test_idempotency.py`, `tests/live/test_live_idempotency.py` |
| Model client (fake SDK), eval script | `tests/model_client/` |
| Hash-locked dependencies, SBOM, CI install | `tests/security/test_dependency_lock.py` |

## Promotion

Nothing in this repo promotes to any other mode. Per CLAUDE.md, promotion requires regression
evals, risk invariants, replay, paper and fault-injection tests to pass **and** an explicit
production approval by the CODEOWNERS on the protected paths.
