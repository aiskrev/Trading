"""The orchestrator's side of the risk-service link. Same interface as RiskGateway (`policy`, `mode`,
`evaluate(...)` -> (decision, order)), so TradingPipeline uses either.

It holds no signing key and cannot make an order: it can only pass on an order the service signed, and it
checks that order against the proposal before passing it on. Everything else fails closed:

  * at construction, `GET /v1/info` must succeed and name the expected mode and an approved policy hash,
    else ConfigError (the run does not start);
  * on any fault during `evaluate` (connection, TLS, timeout, non-200, bad JSON, schema failure, an answer
    that does not match the request) it returns a locally made DENY with a RISK_SERVICE_* reason code and
    no order. It never retries an evaluation: a retry would be a new request with a new request_id.

The execution side still verifies every order (signature, time window, environment, account, policy).
"""
import copy
import http.client
import socket
import ssl
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple

from pydantic import ValidationError

from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.critic import CriticVerdict
from trading_agent.contracts.features import FeatureSet
from trading_agent.contracts.market import MarketSnapshot
from trading_agent.contracts.proposal import OrderProposal
from trading_agent.contracts.risk import RiskDecision, RiskVerdict, RuleResult
from trading_agent.core.clock import Clock
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import IdFactory
from trading_agent.risk_service.protocol import (
    EVALUATE_PATH,
    INFO_PATH,
    LOOPBACK_HOSTS,
    MAX_BODY_BYTES,
    SERVICE_IDENTITY,
    SERVICE_REASON_CODES,
    EvaluateRequest,
    EvaluateResponse,
    ProtocolError,
    RiskResult,
    ServiceError,
    ServiceInfo,
    encode,
    strict_loads,
)

# (method, path, body) -> (status, body). The default is HTTPS with mutual TLS; tests may inject another.
Exchange = Callable[[str, str, Optional[bytes]], Tuple[int, bytes]]


class RemotePolicyView:
    """What the pipeline needs from the policy: its version, hash and a read-only summary for the critic."""

    def __init__(self, info: ServiceInfo) -> None:
        self.version = info.policy_version
        self.source_sha256 = info.policy_sha256
        self._summary = info.policy_summary

    def summary(self) -> Dict[str, Any]:
        return copy.deepcopy(self._summary)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connects to an address but verifies the certificate against the service's name."""

    def __init__(self, host: str, port: int, context: ssl.SSLContext, server_name: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._tls_context = context
        self._server_name = server_name

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self._server_name)
        except BaseException:
            raw.close()
            raise


class RemoteRiskGateway:
    def __init__(self, expected_mode: Mode, approved_policy_hashes: Iterable[str], ids: IdFactory, clock: Clock,
                 host: str = "127.0.0.1", port: int = 0, context: Optional[ssl.SSLContext] = None,
                 timeout_s: float = 3.0, server_name: str = SERVICE_IDENTITY,
                 exchange: Optional[Exchange] = None) -> None:
        if exchange is None:
            if host not in LOOPBACK_HOSTS:
                raise ConfigError(f"the risk service is reached on loopback only, not {host!r}")
            if not isinstance(context, ssl.SSLContext) or context.verify_mode != ssl.CERT_REQUIRED \
                    or not context.check_hostname:
                raise ConfigError("an authenticating TLS client context is required")
        self._host, self._port, self._context = host, port, context
        self._timeout, self._server_name = timeout_s, server_name
        self._exchange: Exchange = exchange or self._https
        self._ids, self._clock = ids, clock
        self._mode = Mode(expected_mode)
        approved: FrozenSet[str] = frozenset(approved_policy_hashes)

        try:
            status, body = self._exchange("GET", INFO_PATH, None)
            if status != 200:
                raise ConfigError(f"risk service refused /v1/info: {_reason(body)}")
            info = ServiceInfo.model_validate(strict_loads(body))
        except ConfigError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException, ProtocolError, ValidationError) as exc:
            raise ConfigError(f"risk service unreachable or invalid at start: {type(exc).__name__}") from None
        if info.environment is not self._mode:
            raise ConfigError(f"risk service runs {info.environment.value}, expected {self._mode.value}")
        if info.policy_sha256 not in approved:
            raise ConfigError(f"risk service policy {info.policy_sha256[:12]} is not an approved policy")
        self._info = info
        self.policy = RemotePolicyView(info)

    # ------------------------------------------------------------------ RiskGateway interface
    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def info(self) -> ServiceInfo:
        return self._info

    @property
    def key_id(self) -> str:
        return self._info.key_id

    @property
    def public_key(self) -> bytes:
        return bytes.fromhex(self._info.public_key_hex)

    @property
    def account_alias(self) -> str:
        return self._info.account_alias

    def evaluate(self, proposal: OrderProposal, critic: Optional[CriticVerdict], market: MarketSnapshot,
                 account: AccountSnapshot, trading_state: TradingState, features: Optional[FeatureSet] = None,
                 broker: Optional[BrokerAccountView] = None) -> RiskResult:
        request_id = self._ids.next("riskreq")
        try:
            request = EvaluateRequest(request_id=request_id, proposal=proposal, critic=critic, market=market,
                                      account=account, trading_state=trading_state, features=features,
                                      broker=broker)
            status, body = self._exchange("POST", EVALUATE_PATH, encode(request))
        except (OSError, ssl.SSLError, http.client.HTTPException):
            return self._deny(proposal, "RISK_SERVICE_UNAVAILABLE")
        except Exception as exc:  # anything unexpected: no order
            return self._deny(proposal, "RISK_SERVICE_ERROR", type(exc).__name__)
        if status != 200:
            code = _reason(body)
            return self._deny(proposal, f"RISK_SERVICE_{code}" if code in SERVICE_REASON_CODES
                              else "RISK_SERVICE_ERROR", f"HTTP {status}")
        try:
            response = EvaluateResponse.model_validate(strict_loads(body))
        except (ProtocolError, ValidationError):
            return self._deny(proposal, "RISK_SERVICE_BAD_RESPONSE")
        problems = _mismatches(response, request_id, proposal, market, account, self._info)
        if problems:
            return self._deny(proposal, "RISK_SERVICE_INCONSISTENT_RESPONSE", ",".join(problems))
        return RiskResult(response.decision, response.order)

    # ------------------------------------------------------------------ helpers
    def _deny(self, proposal: OrderProposal, reason_code: str, detail: str = "") -> RiskResult:
        decision = RiskDecision(
            decision_id=self._ids.next("risk-local"), proposal_id=proposal.proposal_id, verdict=RiskVerdict.DENY,
            requested_notional=proposal.requested_notional or Decimal(0), approved_notional=Decimal(0),
            rule_results=[RuleResult(rule_id="risk_service", passed=False, reason_code=reason_code,
                                     detail=detail[:300])],
            policy_version=self._info.policy_version if hasattr(self, "_info") else "unknown",
            policy_sha256=self._info.policy_sha256 if hasattr(self, "_info") else "0" * 64,
            evaluated_at=self._clock.now())
        return RiskResult(decision, None)

    def _https(self, method: str, path: str, body: Optional[bytes]) -> Tuple[int, bytes]:
        conn = _PinnedHTTPSConnection(self._host, self._port, self._context, self._server_name, self._timeout)
        try:
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(MAX_BODY_BYTES + 1)
            if len(data) > MAX_BODY_BYTES:
                raise http.client.HTTPException("response too large")
            return resp.status, data
        finally:
            conn.close()


def _reason(body: bytes) -> str:
    try:
        return ServiceError.model_validate(strict_loads(body)).reason_code
    except (ProtocolError, ValidationError):
        return "UNREADABLE"


def _mismatches(response: EvaluateResponse, request_id: str, proposal: OrderProposal, market: MarketSnapshot,
                account: AccountSnapshot, info: ServiceInfo) -> List[str]:
    d, o = response.decision, response.order
    out: List[str] = []
    if response.request_id != request_id:
        out.append("request_id")
    if d.proposal_id != proposal.proposal_id:
        out.append("decision.proposal_id")
    if (d.policy_version, d.policy_sha256) != (info.policy_version, info.policy_sha256):
        out.append("decision.policy")
    if d.requested_notional != (proposal.requested_notional or Decimal(0)):
        out.append("decision.requested_notional")
    if d.verdict is RiskVerdict.DENY:
        if o is not None:
            out.append("order_on_deny")
        return out
    if o is None:
        return out + ["missing_order"]
    expected = {
        "decision_id": d.decision_id, "proposal_id": proposal.proposal_id, "symbol": proposal.symbol,
        "side": proposal.side, "limit_price": proposal.limit_price, "notional": d.approved_notional,
        "environment": info.environment, "broker_account_alias": info.account_alias,
        "policy_version": info.policy_version, "policy_sha256": info.policy_sha256,
        "market_snapshot_id": market.snapshot_id, "account_snapshot_id": account.snapshot_id,
        "key_id": info.key_id, "issued_at": d.evaluated_at,
    }
    out += [f"order.{k}" for k, v in expected.items() if getattr(o, k) != v]
    if not isinstance(o.expires_at, datetime) or (o.expires_at - o.issued_at).total_seconds() > info.order_ttl_seconds:
        out.append("order.expires_at")
    return out
