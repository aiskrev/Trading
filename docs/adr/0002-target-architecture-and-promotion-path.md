# ADR 0002: Target architecture and promotion path

Status: **Proposed** (needs owner approval; nothing here changes the mode or any credential)

## Context

Today the system is a single process in BACKTEST: planner, critic, risk gateway, simulated broker
and audit ledger share one memory space, and the order-signing key is an in-process secret
(ADR 0001). The owner supplied a target architecture. This ADR records it, the decisions it
implies, and the conflicts with CLAUDE.md that must be resolved before building.

## Target

| area | target | notes |
|---|---|---|
| model | Claude Sonnet 5.5 through the Agent SDK, structured `OrderProposal`, **no tools**, no broker-write tool | adapter behind the existing `LLMClient`; output still goes through `parse_strict` |
| market data | Massive MCP **during development only**; Massive WebSocket or Databento direct feed in production | MCP replies are data, never instructions; MCP never in the order path or production |
| broker phase 1 | Alpaca Paper | first execution-capable credentials in the system |
| broker phase 2 | Alpaca Live (US only) or IBKR Web/TWS API (global) | decision deferred; `ExecutionPort` keeps both open |
| risk | independent deterministic service, versioned YAML/DB policy, external kill switch | see ADR 0003 |
| state | PostgreSQL-class store, durable event queue, append-only immutable audit archive | hash chain stays; archive adds WORM storage |
| observability | OpenTelemetry metrics, logs, traces; separate financial/risk dashboard | |
| deployment | active/passive, one execution leader, broker reconciliation on every failover | fencing token required |
| promotion | BACKTEST -> REPLAY -> SHADOW -> PAPER -> CANARY_LIVE -> LIMITED_LIVE | FULL_CONFIGURED_LIVE is not on the path |

## Decisions

1. **Keep the pipeline and contracts.** The data, planner, critic, gateway, execution and audit
   boundaries are already the right seams. Splitting into services changes transport and trust, not
   the message contracts (`contracts/`, `schemas/`).
2. **The model stays tool-less.** The SDK client is configured with no tools, no file or network
   access, and one structured output. A tool-capable client is a change under ADR 0001.
3. **Orders are only ever created by the risk service and only executed by the execution service**,
   joined by an asymmetric, short-lived, scoped signature (ADR 0003). No Claude-facing component holds
   or can reach an order-capable credential.
4. **Fail closed is a property of each service, not of the orchestrator.** The risk service stops
   signing, and the execution service refuses to send, when its own inputs are unknown or the kill
   switch is set.
5. **Leader fencing.** Exactly one execution leader holds a monotonically increasing epoch; the
   broker adapter attaches `client_order_id`s derived from `order_id`, and the execution service
   refuses orders carrying an older epoch. Reconciliation against the broker runs on every failover
   before the new leader may send.
6. **Immutable audit.** The local hash chain remains the source of truth during a run; each record
   is also written to a write-once archive (object lock or equivalent). Neither the application
   role nor the planner can modify or delete archived records.

## Mode semantics (to make the promotion path testable)

| mode | market data | decisions | orders |
|---|---|---|---|
| BACKTEST | historical dataset | yes | simulator |
| REPLAY | recorded live feed, original timing | yes | simulator |
| SHADOW | live | yes, persisted as DecisionPackets | **none sent**; hypothetical fills computed from later quotes |
| PAPER | live | yes | broker paper account (Alpaca Paper) |
| CANARY_LIVE | live | yes | live account, tiny fixed risk budget, owner-set |
| LIMITED_LIVE | live | yes | live account, owner-approved limits |

## Conflicts that need an owner decision

1. **Promotion order vs CLAUDE.md "Testing".** The rule says no production promotion unless
   "paper tests pass", yet the path puts SHADOW before PAPER. Proposed resolution: SHADOW sends no
   orders and is therefore not a production promotion; the rule is clarified to apply to promotion into
   any mode that can send orders to a broker. **Proposed amendment (not applied; CLAUDE.md is
   owner-controlled):** replace "No production promotion unless:" with "No promotion into a mode that
   sends orders to a broker (PAPER and above) unless:".
2. **Vendor.** `docs/paper-trading-plan.md` recommends Alpaca for market data; this target uses
   Massive or Databento. That plan is superseded by `docs/roadmap.md` when this ADR is accepted.
3. **Paper broker credentials.** The earlier brief said live execution credentials are forbidden.
   Paper keys can place paper orders. Position: they are execution-capable, held only by the
   execution service, issued through the approved secrets manager, scoped to the paper account, and
   never visible to Claude, prompts, logs, tests or git.
4. **Model change.** Planner and critic evaluations (docs/evals) were designed for a different
   model. The model version is part of every audit record; quality evals must be re-run on Sonnet 5.5
   before SHADOW, and a model change later is a new eval round, not a config edit.
5. **Development MCP.** Using Massive MCP needs one reviewed exception to
   `tests/architecture/test_no_network_code.py`, limited to development tooling outside `src/`.

## Consequences

* Several phases touch `/risk`, `/execution` or credentials; each needs tests plus an explicit
  approval entry (`config/approvals.yaml`) before it counts.
* Splitting services adds operational surface (deploys, certificates, queues) in exchange for
  independence: a compromised planner or orchestrator cannot mint orders.
* IBKR vs Alpaca Live is not decided here; the choice is a Phase 9 gate.

## Unverified assumptions (check before relying on them)

Vendor terms, data licences, whether each vendor's stream provides trading-status/halt messages,
Agent SDK options for disabling tools, Alpaca paper/live API behaviour and account scoping, and
object-lock or equivalent features of the chosen archive. None of these were checked from this
environment.
