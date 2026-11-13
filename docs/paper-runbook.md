# PAPER runbook: from here to the first Alpaca paper session

Status (2026-11-13, APR-0013): every component is built and tested end to end on fakes
(`tests/live/test_paper_session.py`): the Alpaca stream, the market status, the risk service, the Alpaca paper
execution, the live loop, recovery and the run script. What is left is **yours**. Claude may never change the
mode and never handles your keys.

## A. One-time setup (owner)

1. **Alpaca paper account and keys.** Follow steps 1-4 in `docs/alpaca.md`, then check the connection:
   ```bash
   python scripts/alpaca_check.py
   ```
2. **The model.** The Anthropic key goes into the vault as `anthropic-api-key` (`docs/model-client.md`). Then
   run the prompt evaluation (`docs/evals/planner-evaluation.md`), starting with a 3-request smoke test:
   ```bash
   python scripts/run_model_eval.py trading_planner.v1 --split development --limit 3
   ```
   Then run the full sets with `--all` on development and validation. The prompt version you approve is the
   one PAPER uses.
3. **One vault client for the session**, reading both secrets:
   ```bash
   python scripts/vault.py grant paper-session --read alpaca-paper --read anthropic-api-key
   ```
   Keep the printed token for the session shell only.
4. **Certificates for the risk-service link**, outside the repository:
   ```bash
   python scripts/make_dev_certs.py ~/trading-agent-pki
   ```

## B. The PAPER decision (owner only)

These are owner decisions. Claude records an approval only from your own words.

1. **Approval entry for PAPER.** It covers risk-policy.v3 with restricted.v1, the strategy
   (`config/strategies/mean-reversion-demo.v1.yaml` + `src/trading_agent/strategy/triggers.py`), the chosen
   prompt versions and the PAPER composition files. In a session, say for example
   *"Одобрявам PAPER за risk-policy.v3, стратегията и prompt-а ... като APR-00NN"*, and Claude writes it with
   the file hashes.
2. **Permit the mode.** In `src/trading_agent/core/mode.py`, change the set to:
   ```python
   PERMITTED_MODES_THIS_STAGE = frozenset({Mode.BACKTEST, Mode.PAPER})
   ```
   This is a live-deployment change and needs tests plus your approval. The tests that pin
   "only BACKTEST" have to be updated in the same change.
3. **The runtime config** is yours to write. Create `config/runtime.paper.yaml`:
   ```yaml
   mode: PAPER
   deployment_id: paper-wsl-1
   code_commit: <git rev-parse --short HEAD>
   strategy_version: mean-reversion-demo.v1
   risk_policy_version: risk-policy.v3
   model_version: claude-sonnet-5-5
   prompt_versions: {trading_planner: v1, risk_critic: v1}
   ```

## C. Each session

US market hours are 16:30-23:00 Bulgarian time. The first 5 and last 10 minutes are skipped. Use two
terminals in WSL, both inside the repository clone.

**Terminal 1, the risk service:**
```bash
python -m trading_agent.risk_service --config config/runtime.paper.yaml --pki-dir ~/trading-agent-pki --log ~/trading-agent-runs/risk/requests.jsonl
```

**Terminal 2, the session:**
```bash
export TRADING_VAULT_TOKEN=<token from A.3>
```
```bash
python scripts/run_paper_session.py --pki-dir ~/trading-agent-pki --run-dir ~/trading-agent-runs/paper
```

Each cycle (60 s) prints one line. The session ends by itself after the close, or with Ctrl-C.

**Stop all new risk at once:** create the file `ops/KILL_SWITCH` (or `<run-dir>/KILL_SWITCH`):
```bash
touch ops/KILL_SWITCH
```
The risk service and the gate then deny everything. As a last resort, cancel orders in the Alpaca dashboard.

**After a HALT:** read the reason in the last packets or in the control room
(`python -m ui`, then http://127.0.0.1:8765/). A new session resumes only after you acknowledge the HALT
in `halt_ack.json` (`docs/operating-rules.md`).

**Metrics:**
```bash
python -m trading_agent.orchestration.session_metrics ~/trading-agent-runs/paper
```

## What the code refuses, by design

* **Not ready to start:**
  * a mode that is not permitted;
  * a runtime config whose mode is not PAPER;
  * a risk service on another mode, policy or account.
* **Broker account problems:**
  * an Alpaca account that is not ACTIVE, is blocked or is not in USD;
  * positions already open at the start of a new run (close them in the dashboard first).
* **During a session:**
  * any broker answer it cannot interpret: HALT;
  * a restart inside a session: REDUCE_ONLY (exits only);
  * an order still unresolved after a crash: HALT until it is settled with the broker.

## Known limits of this first PAPER version

* The strategy is the demo mean reversion. It exists to exercise the system, not to make money.
* IEX data only, so trading halts are seen only as missing quotes.
* A transient broker read error halts the session (deliberately conservative).
* Exits are capped by `max_order_notional` too, so closing a 5,000 USD position can take two orders.
* The signing key is ephemeral per risk-service start; the vault signer (ADR 0003 step 4) comes later.
