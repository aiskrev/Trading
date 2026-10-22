"""The port between the model client and an SDK.

`ModelRequest` has no field through which a tool, tool choice, MCP server or any other capability could be
offered to the model: the schema is closed (`extra="forbid"`), so "tools disabled" is structural rather than a
setting that could be left on. A transport implementation must send exactly these fields and nothing else.
"""
from typing import List, Optional

from pydantic import Field
from typing_extensions import Protocol

from trading_agent.contracts.base import Contract
from trading_agent.core.errors import TradingAgentError


class ModelRequest(Contract):
    model: str = Field(min_length=1)          # pinned model id, never an alias
    system: str
    user: str                                 # canonical JSON of the planner/critic payload
    max_tokens: int = Field(gt=0, le=8000)
    timeout_s: float = Field(gt=0, le=120)


class ContentBlock(Contract):
    type: str                                 # "text" is the only type the client accepts
    text: Optional[str] = None


class ModelReply(Contract):
    model: str                                # the model that actually answered
    blocks: List[ContentBlock]
    stop_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class ModelTransport(Protocol):
    def send(self, request: ModelRequest) -> ModelReply:
        """Raise a TransportError subclass for timeouts, rate limits, server errors and rejected requests."""


class TransportError(TradingAgentError):
    code = "TRANSPORT_ERROR"
    retryable = False


class TransportTimeout(TransportError):
    code, retryable = "TIMEOUT", True


class TransportRateLimited(TransportError):
    code, retryable = "RATE_LIMITED", True

    def __init__(self, message: str = "", retry_after_s: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TransportServerError(TransportError):
    code, retryable = "SERVER_ERROR", True


class TransportAuthError(TransportError):
    code = "AUTH_ERROR"


class TransportBadRequest(TransportError):
    code = "BAD_REQUEST"
