from pydantic import SecretStr
from typing_extensions import Protocol


class SecretsProvider(Protocol):
    def get(self, name: str) -> SecretStr:
        """Return a secret by logical name. Raise SecretUnavailable if absent. Never log the value."""
