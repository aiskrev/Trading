# Model client (roadmap P2)

`src/trading_agent/model_client/` is the only place a model call is made. It implements the planner/critic `LLMClient`
port (text in, text out) over a narrow `ModelTransport`.

## Pieces

| piece | file | what it guarantees |
|---|---|---|
| `ModelRequest` | `transport.py` | closed schema: `model`, `system`, `user`, `max_tokens`, `timeout_s`, `effort`. No field for tools, tool choice or MCP servers, so none can be offered; a test pins the field set and rejects each extra. |
| `SdkLLMClient` | `client.py` | pinned model id (aliases refused); user message = canonical JSON of the payload; retries only transient errors within a total deadline; strict reply validation; returns the reply text byte for byte. |
| `AnthropicMessagesTransport` | `anthropic_transport.py` | the one module that imports the `anthropic` SDK (lazily; allow-listed in `test_no_network_code`). Key only from a `SecretsProvider`; SDK retries off; every SDK exception becomes a code-only `TransportError`. |
| `evaluation_time` | `planning/planner.py` | the orchestrator's clock, UTC, in every planner payload. |
| eval set `planner-eval.v2` | `evals/planner_quality/` | `v1` plus `evaluation_time`; frozen; holdout sealed. |
| `scripts/run_model_eval.py` | | runs a prompt over the frozen requests with the real model, records the outputs, prints the report. Owner runs it. |

## Why the Messages API and not the Agent SDK

The planner needs one answer to one question. The Agent SDK is Claude Code as a library: Bash, file and web tools are
built in and have to be turned off. With `messages.create` there is nothing to turn off: no `tools` argument is ever
passed and the request type cannot carry one. (ADR 0002 amendment.)

## What is sent, and what deliberately is not

Sent: `model` (exact id `claude-sonnet-5-5`), `max_tokens` (6000; reasoning tokens count against it), `system`, one
user message, `output_config={"effort": "medium"}`. Not sent: `tools`, `tool_choice`, `thinking` (adaptive is the
model's default; `disabled` is rejected by this model), `temperature`/`top_p`/`top_k` (rejected on current models),
betas, `fallbacks`, any `base_url` (the payload can only go to the SDK's default host). Effort `medium` is a first
setting, to be tuned by the evaluation: it trades reasoning depth against latency and cost, and latency matters here
because quotes go stale in seconds.

## Failure mapping (all fail closed)

| situation | client raises | planner result |
|---|---|---|
| timeout, connection error, 429, 5xx (after one bounded retry), auth, bad request, deadline | `LLMUnavailable` | NO_TRADE `LLM_UNAVAILABLE` |
| reply containing a tool-use or any other non-text, non-reasoning block; no text block; several text blocks; cut off by `max_tokens`; refusal | `SchemaFailure` | NO_TRADE `SCHEMA_FAILURE` |
| text that is not exactly one JSON object valid for `ProposalBody` | (parsed by the planner) | NO_TRADE `SCHEMA_FAILURE` |
| answering model differs from the pinned one | `ModelVersionMismatch` | NO_TRADE `MODEL_FAILURE` |

`thinking` / `redacted_thinking` blocks are ignored (the model reasons by default; they carry no action) and the
transport never copies reasoning text anywhere. Critic failures become DENY in the same way. Malformed replies are
not retried. Defaults: 2 attempts, 10 s per attempt, 15 s in total for the pipeline.

Refusal fallbacks to another model are **not** enabled: a refusal is NO_TRADE, and a different answering model would
make `model_version` in the audit record wrong.

## Logs

Only metadata: prompt id and version, model id, attempt, outcome code, latency. Never the system prompt, the payload,
the reply or the key (tested with markers).

## Running it for real (owner, in a terminal; this spends money)

Needs Python >= 3.10 (the `anthropic` 1.x requirement; the WSL Ubuntu has 3.12) and the vault.

```bash
~/venvs/trading/bin/pip install "anthropic>=1,<2"
~/venvs/trading/bin/python scripts/vault.py put-secret anthropic-api-key
~/venvs/trading/bin/python scripts/vault.py grant model-client --read anthropic-api-key
export TRADING_VAULT_TOKEN=...   # the token printed once by `grant`
~/venvs/trading/bin/python scripts/run_model_eval.py trading_planner.v1 --split development --limit 3
```

The script prints an upper bound on the cost and needs a typed `YES`. Recordings go to
`evals/planner_quality/recordings/` (immutable; a stopped run resumes), with a provenance line (model, effort, time).
The holdout split is refused by the script.

## Still open

* The first real call has not been made: nothing in this repo has scored `trading_planner.v1` or the v2 candidate.
  Roadmap P2 acceptance test 4 stays open until it does.
* `prompts/trading_planner.v1.md` still says "data returned by approved tools"; the candidate `v2` removes it and
  already handles `evaluation_time`. Choosing a prompt needs the evaluation and an owner approval.
* The `anthropic` dependency is declared as an optional extra (`model`); it is not hash-locked yet (before PAPER).
