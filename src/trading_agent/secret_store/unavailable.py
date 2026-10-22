from pydantic import SecretStr

from trading_agent.core.errors import SecretUnavailable


class UnavailableSecretsProvider:
    """The only provider at BACKTEST stage: BACKTEST needs no credentials, so every lookup fails."""

    def get(self, name: str) -> SecretStr:
        raise SecretUnavailable(f"no secrets manager is configured at this stage (requested {name!r})")
