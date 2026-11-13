"""Broker account alias per mode. The alias is bound into every signed order and into client_order_id, so an
order signed for one account is refused by any other (ADR 0003 check 4)."""
from typing import Dict

from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode
from trading_agent.core.signing import BACKTEST_ACCOUNT_ALIAS

ALPACA_PAPER_ALIAS = "alpaca-paper"
ACCOUNT_ALIASES: Dict[Mode, str] = {Mode.BACKTEST: BACKTEST_ACCOUNT_ALIAS, Mode.PAPER: ALPACA_PAPER_ALIAS}


def account_alias_for(mode: Mode) -> str:
    try:
        return ACCOUNT_ALIASES[Mode(mode)]
    except KeyError:
        raise ConfigError(f"no broker account is configured for {Mode(mode).value}") from None
