from pydantic import SecretStr
from typing_extensions import Protocol


class SecretsProvider(Protocol):
    def get(self, name: str) -> SecretStr:
        """Return a secret by logical name. Raise SecretUnavailable if absent. Never log the value."""


class Signer(Protocol):
    """Sign-only access to one signing key. There is deliberately no way to obtain the private key.

    Implementations: the in-house vault today; a managed KMS/HSM before CANARY_LIVE (same interface).
    """

    key_id: str

    def sign(self, message: bytes) -> bytes: ...

    def public_key(self) -> bytes: ...
