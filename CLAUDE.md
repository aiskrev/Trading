# Trading Agent Project Rules

## Safety hierarchy
Correctness > capital preservation > availability > performance > convenience.

## Production modes
BACKTEST
REPLAY
PAPER
SHADOW
CANARY_LIVE
LIMITED_LIVE
FULL_CONFIGURED_LIVE

Claude may NEVER change the active mode.

## Credential policy
Never place API keys, broker passwords, OAuth refresh tokens,
private keys, account recovery codes, or MFA secrets in:
- prompts
- CLAUDE.md
- source files
- tests
- logs
- screenshots
- git history

Use the approved secrets manager only.

## Broker rule
No Claude-facing tool may expose unrestricted raw broker order submission.

## Risk rule
All executable orders MUST pass deterministic risk validation.

## Code changes
Any change touching:
- /risk
- /execution
- credentials
- live deployment
- broker permissions
requires tests and an explicit production approval step.

## Failure policy
On ambiguity, stale data, model failure, schema failure, data-feed
divergence, broker reconciliation mismatch, or unknown market state:
FAIL CLOSED for new risk.

## Testing
No production promotion unless:
- regression evals pass;
- risk invariants pass;
- replay tests pass;
- paper tests pass;
- fault-injection tests pass.

## Auditability
Every decision must be attributable to:
model version,
prompt version,
strategy version,
market snapshot,
risk-policy version,
code commit,
deployment ID,
broker result.
