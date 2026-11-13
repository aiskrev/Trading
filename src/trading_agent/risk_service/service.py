"""Request logic of the risk service (ADR 0003, step 3). No network code: server.py calls this.

Order of checks for `POST /v1/evaluate` (each failure answers an error and never an order):
  1. the TLS peer is the orchestrator identity;
  2. the rate limit;
  3. body size, strict JSON (no duplicate keys, no NaN) and the closed request schema;
  4. the request_id has not been seen (a replayed request never yields a second order);
  5. RiskGateway.evaluate, with the kill-switch rule first among the gate rules;
  6. the request record is appended to the hash-chained request log BEFORE the answer leaves the process;
     if that write fails the order is discarded and AUDIT_WRITE_FAILURE is returned.

The log holds hashes, ids and verdicts, never a signature or a key.
"""
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple, Union

from pydantic import Field, ValidationError

from trading_agent.audit.chain import ChainedLog
from trading_agent.contracts.base import Contract
from trading_agent.contracts.risk import RuleResult
from trading_agent.core.clock import Clock
from trading_agent.core.errors import ConfigError
from trading_agent.core.hashing import sha256_hex
from trading_agent.core.mode import Mode, assert_mode_permitted
from trading_agent.core.signing import BACKTEST_ACCOUNT_ALIAS, OrderTiming
from trading_agent.core.types import IdFactory
from trading_agent.risk.approvals import require_approval
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk.policy import RiskPolicy
from trading_agent.risk.rules import CAPS, GATE_RULES
from trading_agent.risk.rules.context import RuleContext, fail
from trading_agent.risk_service.protocol import (
    MAX_BODY_BYTES,
    ORCHESTRATOR_IDENTITY,
    EvaluateRequest,
    EvaluateResponse,
    ProtocolError,
    ServiceError,
    ServiceInfo,
    encode,
    strict_loads,
)
from trading_agent.secret_store.ports import Signer

Answer = Tuple[int, bytes]
REQUEST_IDS_REMEMBERED = 100_000
_stat = os.stat   # indirection so a test can make the kill-switch state unreadable


class RequestRecord(Contract):
    """One line of the service's request log (hash-chained). Never a signature, key or payload."""

    at: datetime
    endpoint: str
    peer: str = Field(max_length=64)
    outcome: str                               # ANSWERED | REFUSED
    status: int
    reason_code: Optional[str] = None
    request_id: Optional[str] = None
    request_sha256: Optional[str] = None       # hash of the raw body as received
    proposal_id: Optional[str] = None
    verdict: Optional[str] = None
    decision_id: Optional[str] = None
    order_id: Optional[str] = None
    policy_sha256: str


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int, monotonic: Callable[[], float] = time.monotonic) -> None:
        if rate_per_s <= 0 or burst < 1:
            raise ConfigError("rate limit must be positive")
        self._rate, self._burst, self._now = rate_per_s, float(burst), monotonic
        self._tokens, self._last = float(burst), monotonic()

    def take(self) -> bool:
        now = self._now()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False


def kill_switch_rule(paths: Sequence[Path]) -> Callable[[RuleContext], RuleResult]:
    """Layer 1 of the external kill switch (ADR 0003): the risk service stops signing.

    The switch is ACTIVE if any of the files exists, or if its state cannot be read (fail closed).
    """
    paths = tuple(Path(p) for p in paths)

    def check(ctx: RuleContext) -> RuleResult:
        for p in paths:
            try:
                _stat(p)
            except FileNotFoundError:
                continue
            except OSError:
                return fail("kill_switch", "KILL_SWITCH_UNREADABLE", p.name)
            return fail("kill_switch", "KILL_SWITCH_ACTIVE", p.name)
        return RuleResult(rule_id="kill_switch", passed=True)

    check.__name__ = "check"
    check.__module__ = "kill_switch"
    return check


class RiskService:
    def __init__(self, gateway: RiskGateway, signer: Signer, log: ChainedLog, clock: Clock, service_id: str,
                 account_alias: str = BACKTEST_ACCOUNT_ALIAS, timing: OrderTiming = OrderTiming(),
                 limiter: Optional[TokenBucket] = None) -> None:
        self._gateway = gateway
        self._log = log
        self._clock = clock
        self._limiter = limiter or TokenBucket(rate_per_s=20, burst=40)
        self._seen: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()
        policy = gateway.policy
        self._info = ServiceInfo(service_id=service_id, environment=gateway.mode, account_alias=account_alias,
                                 policy_version=policy.version, policy_sha256=policy.source_sha256,
                                 policy_summary=policy.summary(), key_id=signer.key_id,
                                 public_key_hex=signer.public_key().hex(), order_ttl_seconds=timing.ttl_seconds)

    @property
    def info(self) -> ServiceInfo:
        return self._info

    # ------------------------------------------------------------------ endpoints
    def handle_info(self, peer: str) -> Answer:
        with self._lock:
            if peer != ORCHESTRATOR_IDENTITY:
                return self._refuse("info", peer, 403, "PEER_NOT_ALLOWED")
            if not self._limiter.take():
                return self._refuse("info", peer, 429, "RATE_LIMITED")
            if not self._record("info", peer, "ANSWERED", 200):
                return self._error(503, "AUDIT_WRITE_FAILURE")
            return 200, encode(self._info)

    def handle_evaluate(self, body: bytes, peer: str) -> Answer:
        with self._lock:
            return self._evaluate(body, peer)

    def refuse(self, endpoint: str, peer: str, status: int, reason_code: str) -> Answer:
        """For refusals decided by the transport layer (bad path, media type, size, failed handshake)."""
        with self._lock:
            return self._refuse(endpoint, peer, status, reason_code)

    # ------------------------------------------------------------------ internals
    def _evaluate(self, body: bytes, peer: str) -> Answer:
        ep = "evaluate"
        digest = sha256_hex(bytes(body))
        if peer != ORCHESTRATOR_IDENTITY:
            return self._refuse(ep, peer, 403, "PEER_NOT_ALLOWED", request_sha256=digest)
        if not self._limiter.take():
            return self._refuse(ep, peer, 429, "RATE_LIMITED", request_sha256=digest)
        if len(body) > MAX_BODY_BYTES:
            return self._refuse(ep, peer, 413, "BODY_TOO_LARGE", request_sha256=digest)
        try:
            raw = strict_loads(body)
        except ProtocolError:
            return self._refuse(ep, peer, 400, "BAD_JSON", request_sha256=digest)
        try:
            request = EvaluateRequest.model_validate(raw)
        except ValidationError:
            return self._refuse(ep, peer, 400, "SCHEMA_FAILURE", request_sha256=digest)
        rid = request.request_id
        if rid in self._seen:
            return self._refuse(ep, peer, 409, "DUPLICATE_REQUEST", request_id=rid, request_sha256=digest)
        self._remember(rid)
        try:
            result = self._gateway.evaluate(request.proposal, request.critic, request.market, request.account,
                                            request.trading_state, request.features, request.broker)
            answer = encode(EvaluateResponse(request_id=rid, decision=result.decision, order=result.order))
        except Exception:  # signer or serialisation failure: no order leaves the process
            return self._refuse(ep, peer, 500, "INTERNAL_ERROR", request_id=rid, request_sha256=digest)
        d = result.decision
        if not self._record(ep, peer, "ANSWERED", 200, request_id=rid, request_sha256=digest,
                            proposal_id=request.proposal.proposal_id, verdict=d.verdict.value,
                            decision_id=d.decision_id, order_id=result.order.order_id if result.order else None):
            return self._error(503, "AUDIT_WRITE_FAILURE", rid)
        return 200, answer

    def _remember(self, request_id: str) -> None:
        self._seen[request_id] = None
        while len(self._seen) > REQUEST_IDS_REMEMBERED:
            self._seen.popitem(last=False)

    def _refuse(self, endpoint: str, peer: str, status: int, reason_code: str, request_id: Optional[str] = None,
                **fields) -> Answer:
        self._record(endpoint, peer, "REFUSED", status, reason_code=reason_code, request_id=request_id, **fields)
        return self._error(status, reason_code, request_id)

    @staticmethod
    def _error(status: int, reason_code: str, request_id: Optional[str] = None) -> Answer:
        return status, encode(ServiceError(request_id=request_id, reason_code=reason_code))

    def _record(self, endpoint: str, peer: str, outcome: str, status: int, **fields) -> bool:
        try:
            self._log.append(RequestRecord(at=self._clock.now(), endpoint=endpoint, peer=(peer or "-")[:64],
                                           outcome=outcome, status=status,
                                           policy_sha256=self._info.policy_sha256, **fields))
            return True
        except Exception:  # cannot log => cannot answer with an order
            return False


def build_risk_service(policy: RiskPolicy, mode: Mode, signer: Signer, clock: Clock,
                       config_dir: Union[str, Path], log_path: Union[str, Path], service_id: str,
                       kill_switch_paths: Iterable[Union[str, Path]] = (),
                       limiter: Optional[TokenBucket] = None, timing: OrderTiming = OrderTiming(),
                       account_alias: str = BACKTEST_ACCOUNT_ALIAS) -> RiskService:
    """Refuses to build unless the mode is permitted and the policy is approved for it (fail closed)."""
    assert_mode_permitted(mode)
    require_approval(policy, mode, config_dir)
    log, opened = ChainedLog.open(log_path, RequestRecord)
    if opened.torn_line_moved_to is not None:
        raise ConfigError(f"request log had a torn final line, moved to {opened.torn_line_moved_to.name}; "
                          "inspect it before starting the service")
    gateway = RiskGateway(policy, mode, signer, IdFactory(service_id), clock,
                          gate_rules=(kill_switch_rule(list(kill_switch_paths)),) + tuple(GATE_RULES), caps=CAPS,
                          account_alias=account_alias, timing=timing)
    return RiskService(gateway, signer, log, clock, service_id, account_alias, timing, limiter)
