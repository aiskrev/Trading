# ADR 0001: The LLM planner never executes; a deterministic gateway mints signed orders

Status: Accepted (stage 1, BACKTEST)

## Context
CLAUDE.md requires that no Claude-facing tool exposes unrestricted order submission and that all
executable orders pass deterministic risk validation. LLM output is non-deterministic and can be
influenced by untrusted content (news, filings, tool responses).

## Decision
1. The planner and critic are text-in/text-out `LLMClient`s with **no tools**.
2. Their output is parsed strictly into `ProposalBody` / `CriticBody`; failures become NO_TRADE / DENY.
3. `RiskGateway` is the only component that can produce an `ApprovedOrder`, signed with an
   in-process HMAC key. `ExecutionPort.submit` accepts nothing else and verifies the signature.
4. `import-linter` forbids `planning` from importing `execution`, `risk`, `core.signing`,
   `secret_store`, `orchestration` or `backtest`.
5. Production `Mode` is frozen config, separate from the fail-closed `TradingState`.

## Consequences
* A compromised or confused model can at worst cause a DENY or a NO_TRADE, or a trade fully
  within deterministic limits.
* Adding a real broker adapter means adding a new `ExecutionPort` in `execution/registry.py`,
  which is CODEOWNERS-protected and requires production approval.
* The critic adds latency and cost but can only tighten.
