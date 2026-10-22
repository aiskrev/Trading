# Model client (roadmap P2)

`src/trading_agent/model_client/` is the only place a model call is made. It implements the
planner/critic `LLMClient` port (text in, text out) over a narrow `ModelTransport`.

## What is built (BACKTEST, offline)

| piece | file | what it guarantees |
|---|---|---|
| `ModelRequest` | `transport.py` | closed schema: `model`, `system`, `user`, `max_tokens`, `timeout_s`. There is **no field** for tools, tool choice, MCP servers or allowed tools, so none can be offered; a test pins the field set and rejects each extra. |
| `SdkLLMClient` | `client.py` | pinned model id (aliases refused); user message = canonical JSON of the payload; retries only transient errors within a total deadline; returns the reply text byte for byte. |
| `evaluation_time` | `planning/planner.py` | the orchestrator's clock, UTC, in every planner payload (the tick passes `clock.now()`), so the model can verify snapshot age. |
| eval set `planner-eval.v2` | `evals/planner_quality/` | `v1` plus `evaluation_time`; frozen; holdout sealed. |

## Failure mapping (all fail closed)

| situation | client raises | planner result |
|---|---|---|
| timeout, 429, 5xx (after one bounded retry), auth, bad request, deadline | `LLMUnavailable` | NO_TRADE `LLM_UNAVAILABLE` |
| reply with a tool-use / thinking / any non-text block, several blocks, empty, cut off by `max_tokens`, refusal | `SchemaFailure` | NO_TRADE `SCHEMA_FAILURE` |
| text that is not exactly one JSON object valid for `ProposalBody` | (parsed by the planner) | NO_TRADE `SCHEMA_FAILURE` |
| answering model differs from the pinned one | `ModelVersionMismatch` | NO_TRADE `MODEL_FAILURE` |

Critic failures become DENY in the same way. Malformed replies are not retried. Defaults: 2 attempts, 10 s per
attempt, 15 s in total (a slow model must not outlive the data: quotes are stale after seconds; the gateway
re-checks freshness after the model returns).

## Logs

Only metadata: prompt id and version, model id, attempt, outcome code, latency. Never the system prompt, the
payload or the reply (tested with markers).

## Not built yet (each is its own step and approval)

* **The real SDK transport.** It needs network access (the architecture test `test_no_network_code` forbids it
  today, so it would need a narrow allow-list), the Claude Agent SDK configured with **no tools**, and a Python
  newer than the 3.9 installed here (the SDK's requirement should be checked first). It must read the API key
  only through a `SecretsProvider` (the vault, once created by the owner) and send exactly the `ModelRequest` fields.
* **Quality evaluation of Sonnet 5.5.** `python -m evals.planner_quality.run export <label> --split development`
  writes the requests; the outputs must come from a real model in an approved environment. Nothing in this repo has
  scored `trading_planner.v1` or the v2 candidate yet. Roadmap P2 acceptance test 4 stays open until it does.
* **Prompt text.** `prompts/trading_planner.v1.md` still says "data returned by approved tools"; the candidate
  `v2` removes it and already handles `evaluation_time`. Choosing a prompt needs the evaluation above and an approval.
