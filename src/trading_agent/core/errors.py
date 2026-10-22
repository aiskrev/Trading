class TradingAgentError(Exception):
    """Base class for all project errors."""


class ConfigError(TradingAgentError):
    pass


class ModeNotPermitted(TradingAgentError):
    """Raised when the configured mode is not allowed at this stage."""


class SchemaFailure(TradingAgentError):
    """Model or tool output failed strict schema validation."""


class LLMUnavailable(TradingAgentError):
    """LLM call failed, timed out, or has no recorded response."""


class ForgedOrder(TradingAgentError):
    """An order reached execution without a valid gateway signature."""


class DuplicateOrder(TradingAgentError):
    pass


class AdapterNotPermitted(TradingAgentError):
    """No execution adapter is permitted for the requested mode."""


class SecretUnavailable(TradingAgentError):
    pass


class AuditWriteFailure(TradingAgentError):
    pass


class UnknownInstrument(TradingAgentError):
    """A vendor symbol that does not map to a configured instrument. Never guessed."""


class TransportError(TradingAgentError):
    """Connection lost, timeout, or upstream 5xx. Messages never include payloads or credentials."""


class RateLimited(TradingAgentError):
    def __init__(self, retry_after_s=None, local: bool = False) -> None:
        super().__init__(f"rate limited (retry_after_s={retry_after_s}, local={local})")
        self.retry_after_s = retry_after_s
        self.local = local


class AuthRejected(TradingAgentError):
    """Upstream rejected our credentials (401/403 or auth error message)."""


class ReadOnlyViolation(TradingAgentError):
    """A data adapter attempted a request outside its read-only allow-list."""


class VaultError(TradingAgentError):
    """Vault could not be opened, is corrupted/tampered, or an operation failed. Fail closed."""


class VaultDenied(VaultError):
    """A vault client was not allowed to do what it asked. The reason is logged, never the value."""
