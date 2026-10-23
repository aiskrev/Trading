# Control room

A local web page for working with this project without reading files and logs by hand. It shows the state of the
system, the decisions the live loop made, backtest and evaluation results, the approvals and the rules, and it can run the
project's standing checks. Standard library only; no new dependency.

## Start it

Windows (double-click or from a terminal):

```bat
ui\start.cmd
```

Linux / WSL, with the project's virtual environment:

```bash
~/venvs/trading/bin/python -m ui
```

Then open <http://127.0.0.1:8765/> (it opens by itself). Options: `--port 8766`, `--no-browser`,
`--root <project folder>`, `--runs-dir <folder with run directories>`. To look at runs made inside WSL from Windows, point
`--runs-dir` at the WSL folder, for example `\\wsl.localhost\Ubuntu-22.04\home\<user>\trading-agent\runs`.

## What is on each page

| page | what it shows | what it can start |
|---|---|---|
| Overview | mode, kill switch, approvals (valid / superseded / voided, checked live against file hashes), git state, versions in use, latest checks | nothing |
| Decisions | every DecisionPacket of a live-loop run: outcome, proposal, risk verdict and rule results, order and fills, raw model output, timings; equity chart; hash-chain checks of the packets, ledger and idempotency files; restarts and recoveries | a demo run on mock feeds (BACKTEST, simulated broker) |
| Backtests | dataset list, existing reports (rendered), and the report of a new run | a backtest on a dataset under `data/datasets/` |
| Model & evals | pinned model, prompts and their hashes, real-run provenance, planner-evaluation reports, scenario explorer (development and validation only) with the model outputs recorded for each scenario | nothing: it prints the terminal commands for the paid runs |
| Approvals & rules | every approval with its files and their current state, the active risk policy, ops policy, execution assumptions, strategies | nothing |
| Checks | tests, module boundaries, schema drift, released risk config, SBOM, guardrail evals | any of them, or all |
| Docs | the project's Markdown documents and configuration files, read-only | nothing |

## What it will not do (and tests make sure of it)

* **It cannot change** the mode, any config file, an approval, a prompt, the kill switch or a secret. There is no route
  for it. The only POST starts one of eight fixed jobs.
* **It cannot place an order or call the model**, and it imports no execution, risk-gateway, signing, vault, model-client
  or market-feed code. Paid model runs stay in a terminal, because they need the vault passphrase, which no web page
  should ever see.
* **It listens on 127.0.0.1 only**, checks the `Host` and `Origin` headers (DNS rebinding, cross-site requests), needs a
  per-start token for the one POST, sends a strict Content-Security-Policy, loads nothing from the internet and puts data
  on the page as text, never as markup.
* **Jobs run with a cleaned environment**: anything whose name looks like a token, key, secret or password is removed
  before a child process starts, and their output is redacted before it is shown.
* **Verification is read-only.** Checking a hash chain does not move a torn last line aside, as the real loader would.
* **The sealed holdout split is never exposed.**

## Files

```
ui/server.py      HTTP server, routing, header checks
ui/data.py        read-only data access (approvals, runs, packets, chains, evals, docs)
ui/jobs.py        the fixed list of jobs, cleaned environment, redaction
ui/make_demo_run.py   creates runs/demo-live on mock feeds (run as its own process)
ui/static/        index.html, app.js, style.css (no framework, no external assets)
tests/ui/         security and data tests
```

The control room writes only `runs/ui/last_checks.json` (the last result of each check) plus what the commands it starts
write themselves (`reports/backtests/...`, `runs/demo-live/...`). `runs/` is git-ignored.
