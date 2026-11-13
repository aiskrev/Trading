"""Wire schemas for the risk service (ADR 0003, step 3). No network code here.

The request carries only what `RiskGateway.evaluate` takes. There is no field for a mode, a policy, a
threshold, a key or an account: those are fixed in the service at start, so a request cannot change them
(`extra="forbid"` turns any extra field into a schema failure).
"""
import json
from decimal import Decimal
from typing import Any, Dict, NamedTuple, Optional

from pydantic import Field
from typing_extensions import Literal

from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.base import Contract
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.execution import ApprovedOrder
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import OrderProposal
from trading_agent.contracts.risk import RiskDecision
from trading_agent.core.hashing import canonical_json
from trading_agent.core.mode import Mode
from trading_agent.core.trading_state import TradingState

PROTOCOL_VERSION = 1
MAX_BODY_BYTES = 512 * 1024
SERVICE_IDENTITY = "risk-service"       # CN and DNS SAN of the service certificate
ORCHESTRATOR_IDENTITY = "orchestrator"  # the only client identity the service answers
INFO_PATH = "/v1/info"
EVALUATE_PATH = "/v1/evaluate"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Reason codes the service may return in a ServiceError (anything else is reported as RISK_SERVICE_ERROR).
SERVICE_REASON_CODES = frozenset({
    "BAD_JSON", "SCHEMA_FAILURE", "BODY_TOO_LARGE", "UNSUPPORTED_MEDIA_TYPE", "PEER_NOT_ALLOWED", "NOT_FOUND",
    "RATE_LIMITED", "DUPLICATE_REQUEST", "AUDIT_WRITE_FAILURE", "INTERNAL_ERROR",
})


class ProtocolError(ValueError):
    """A message is not valid JSON for this protocol (duplicate keys, NaN, wrong shape)."""


class EvaluateRequest(Contract):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str = Field(min_length=1, max_length=128)
    proposal: OrderProposal
    critic: Optional[CriticVerdict] = None
    market: MarketSnapshot
    account: AccountSnapshot
    trading_state: TradingState
    features: Optional[FeatureSet] = None
    broker: Optional[BrokerAccountView] = None


class EvaluateResponse(Contract):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: str
    decision: RiskDecision
    order: Optional[ApprovedOrder] = None


class ServiceInfo(Contract):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    service_id: str = Field(min_length=1)
    environment: Mode
    account_alias: str = Field(min_length=1)
    policy_version: str
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_summary: Dict[str, Any]
    key_id: str = Field(min_length=1)
    alg: Literal["Ed25519"] = "Ed25519"
    public_key_hex: str = Field(pattern=r"^[0-9a-f]{64}$")   # the public half only
    order_ttl_seconds: float = Field(gt=0, le=10)


class ServiceError(Contract):
    protocol_version: Literal[1] = PROTOCOL_VERSION
    request_id: Optional[str] = None
    reason_code: str = Field(pattern=r"^[A-Z_]{3,40}$")


class RiskResult(NamedTuple):
    """Same shape as risk.gateway.GatewayResult, so the pipeline can use either."""

    decision: RiskDecision
    order: Optional[ApprovedOrder]


def encode(model: Contract) -> bytes:
    return canonical_json(model.model_dump(mode="json")).encode("utf-8")


def strict_loads(body: bytes) -> Dict[str, Any]:
    """Parse one JSON object; refuse duplicate keys, NaN/Infinity, floats for money and non-objects."""
    def no_duplicates(pairs):
        out: Dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise ProtocolError(f"duplicate key {k!r}")
            out[k] = v
        return out

    def no_constants(name):
        raise ProtocolError(f"non-finite number {name}")

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=no_duplicates, parse_constant=no_constants,
                           parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(type(exc).__name__) from None
    if not isinstance(value, dict):
        raise ProtocolError("top level must be an object")
    return value
