# Architecture

Stage: **BACKTEST only**. Governing policy: [`CLAUDE.md`](../CLAUDE.md).

## Data and control flow

```
MarketDataSource (csv_source)  ─┐
secondary source (optional)    ─┴─> Normalizer ──> MarketSnapshot (frozen, content-hashed)
                                        │ DataIssues ──> fail_closed escalation
                                        v
                                  FeaturePipeline ──> FeatureSet
                                        v
           Portfolio.snapshot() ──> AccountSnapshot (frozen, content-hashed)
                                        v
                ClaudePlanner (LLMClient, no tools) ──> OrderProposal   [any failure => NO_TRADE]
                                        v
                ClaudeCritic  (LLMClient, no tools) ──> CriticVerdict   [any failure => DENY]
                                        v
                RiskGateway (deterministic, holds signer) ──> RiskDecision + ApprovedOrder?
                                        v
                AuditLedger.append(DECISION)   <── write-ahead; failure => HALTED, no submit
                                        v
                ExecutionPort.submit(ApprovedOrder)  (BACKTEST: SimulatedBroker, verifies HMAC)
                                        v
                AuditLedger.append(EXECUTION) ──> Reconciler (internal vs broker) ── mismatch => HALTED
                                        v
                PostTradeAuditor (deterministic, 9 sections, evidence = record hashes)
```

## Module responsibilities

| Package | Responsibility | May import |
|---|---|---|
| `core` | types, frozen `RuntimeConfig`/`Mode`, `TradingState`, clock, hashing, `OrderSigner` | — |
| `contracts` | every cross-module message as a frozen, `extra="forbid"` pydantic model | `core` |
| `market_data` | raw ticks, CSV source, instrument registry, streaming adapter, decoder, backoff, rate limit, sequence, quote book | `contracts`, `core`, `observability` |
| `account_data` | read-only REST account adapter, GET-only HTTP client | `market_data`, `contracts`, `core`, `observability` |
| `normalization` | validate ticks, cross-source divergence, build `MarketSnapshot` | `market_data`, `contracts` |
| `features` | point-in-time features, no look-ahead | `contracts` |
| `planning` | Claude planner + critic, strict output parsing, offline LLM clients | `contracts`, `core` (not signing) |
| `risk` | policy + ordered pure rules + caps, mints signed `ApprovedOrder` | `contracts`, `core` |
| `execution` | adapter registry (BACKTEST → `SimulatedBroker` only), cost model | `contracts`, `core` |
| `reconciliation` | internal vs broker positions | `contracts` |
| `audit` | hash-chained ledger, post-trade auditor | `contracts`, `core` |
| `orchestration` | one decision cycle, fail-closed escalation | everything above |
| `backtest` | timeline replay, portfolio, engine, CLI | everything above |
| `observability` | redacting logger, metrics, tracing | — |
| `secret_store` | `SecretsProvider` port; stage-1 provider always raises | `core` |

Boundaries are enforced by `import-linter` contracts in `pyproject.toml` (AT-01).

## Read-only data adapters

```
StreamTransport (mock only) -> GenericJsonQuoteDecoder -> SequenceTracker -> QuoteBook -> fresh_ticks() -> Normalizer
     ^ connect(token from SecretsProvider), reconnect: ExponentialBackoff (equal jitter), Retry-After
HttpTransport (GET only, mock only) -> ReadOnlyHttpClient (allow-list, TokenBucket) -> RestAccountAdapter -> AccountRecord -> AccountSnapshot
```

* **Common records.** `QuoteRecord` and `AccountRecord` always carry `source_id`, the source's
  timestamp (`source_ts`) and our receive time (`received_at`).
* **Instrument ids.** `InstrumentRegistry` (config/instruments.v1.yaml) maps per-source vendor
  symbols to canonical `Symbol`s; unknown symbols are rejected. Its symbol set must equal the risk universe.
* **Acceptance.** The quote book rejects late, future-dated and out-of-order quotes, quarantines on
  sequence gaps until a snapshot, and only serves quotes younger than `max_age`. Disconnects,
  heartbeat silence and rate limits clear the book.
* **Account all-or-nothing.** Any invalid part of an account payload rejects the whole fetch.
* **No network code yet.** Transports are injected; only in-memory mocks exist. Vendor/broker
  transports are a separate, reviewed change. Broker execution stays `SimulatedBroker` only.

## Key design decisions

* **Mode ≠ TradingState.** `Mode` is deployment config, frozen, with no setter. Fail-closed
  behaviour uses a separate `TradingState` (NORMAL → REDUCE_ONLY → HALTED, escalate-only).
* **Capability-signed orders.** The gateway signs `ApprovedOrder` with an HMAC key generated
  in-process at startup (never persisted, not picklable). Execution rejects anything else.
  The planner cannot import the signer.
* **The model never writes attribution.** The LLM returns `ProposalBody`; code wraps it into
  `OrderProposal` with ids, snapshot ids, model/prompt/strategy versions.
* **Evidence refs are checked, not trusted.** Every ref must resolve to something in the
  market/account/feature snapshots, and a TRADE must cite its own quote.
* **Determinism.** Simulated clock, run-scoped sequential IDs, recorded/scripted LLMs and
  canonical JSON hashing make a replay produce a byte-identical audit ledger (AT-16).
* **Long-only, limit-only, IOC** in the simulator at this stage.

## Not in this stage

No live/paper broker adapter, no live Anthropic client, no secrets manager integration,
no promotion tooling. Each of those touches credentials, broker permissions or live
deployment and needs tests plus an explicit production approval.
