"""Gate rules (pass/fail) run in order; then caps compute the approved size.

Every rule is a pure function of RuleContext. A rule that raises is treated as failed (fail
closed). Every threshold is read from ctx.policy (immutable, versioned, hash-pinned config).

Coverage of the pre-trade checklist:
  allowed universe .................. universe
  market / session state ............ market_state (venue), session (calendar)
  max order notional ................ notional cap, min_notional (gateway)
  max position exposure ............. position cap
  gross / net exposure .............. exposure.gross_cap, exposure.net_cap
  sector / concentration ............ exposure.sector_cap, exposure.concentration_cap
  duplicate / open-order conflicts .. duplicate
  price collars ..................... price_collar
  spread / slippage policy .......... spread, slippage
  quote freshness ................... freshness.check (snapshots), freshness.check_quote
  daily loss / drawdown ............. loss_limit, drawdown
  order-rate limits ................. rate (day, minute, instrument-minute)
  broker / account consistency ...... broker_consistency, exposure.buying_power_cap
  halt / restricted instruments ..... instrument_status
"""
from trading_agent.risk.rules import (
    broker_consistency,
    critic,
    drawdown,
    duplicate,
    evidence,
    exposure,
    freshness,
    instrument_status,
    loss_limit,
    market_state,
    mode,
    notional,
    position,
    price_collar,
    rate,
    schema,
    session,
    slippage,
    spread,
    state,
    universe,
)

GATE_RULES = (
    mode.check,
    state.check,
    schema.check,
    universe.check,
    broker_consistency.check,
    freshness.check,
    freshness.check_quote,
    session.check,
    market_state.check,
    instrument_status.check,
    evidence.check,
    price_collar.check,
    spread.check,
    slippage.check,
    duplicate.check,
    loss_limit.check,
    drawdown.check,
    rate.check,
    critic.check,
)

CAPS = (
    notional.cap,
    position.cap,
    exposure.gross_cap,
    exposure.net_cap,
    exposure.sector_cap,
    exposure.concentration_cap,
    exposure.buying_power_cap,
    critic.cap,
)
