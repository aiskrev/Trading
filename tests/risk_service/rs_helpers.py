"""Builders for the risk-service tests: an in-memory test CA (unlike the dev PKI, tests keep the CA key so they
can issue deliberately wrong certificates), inputs that the gateway approves, and a running service."""
import json
import ssl
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from trading_agent.audit.chain import ChainedLog
from trading_agent.contracts.account import AccountSnapshot, BrokerAccountView
from trading_agent.contracts.critic import CriticVerdict, CriticVerdictKind
from trading_agent.contracts.proposal import Decision, OrderProposal, OrderType
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.mode import Mode
from trading_agent.core.signing import EphemeralSigner
from trading_agent.core.types import IdFactory, Side
from trading_agent.risk_service.client import RemoteRiskGateway
from trading_agent.risk_service.pki import issue_leaf
from trading_agent.risk_service.protocol import ORCHESTRATOR_IDENTITY, SERVICE_IDENTITY
from trading_agent.risk_service.server import RiskServiceServer
from trading_agent.risk_service.service import RequestRecord, build_risk_service
from trading_agent.risk_service.tls import client_context, server_context
from trading_agent.testing.harness import ROOT, T0, market, risk_policy

POLICY = risk_policy()
CONFIG_DIR = ROOT / "config"
D = Decimal
SERVER = ExtendedKeyUsageOID.SERVER_AUTH
CLIENT = ExtendedKeyUsageOID.CLIENT_AUTH


class TestCA:
    __test__ = False

    def __init__(self, directory: Path, name: str = "test CA") -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        self.cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                     .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                     .not_valid_before(now).not_valid_after(now + timedelta(days=2))
                     .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                     .sign(self.key, hashes.SHA256()))
        self.ca_file = self.dir / "ca.pem"
        self.ca_file.write_bytes(self.cert.public_bytes(serialization.Encoding.PEM))

    def leaf(self, identity: str, usage, file_stem: str = None):
        now = datetime.now(timezone.utc) - timedelta(minutes=5)
        cert, key = issue_leaf(self.cert, self.key, identity, usage, now, now + timedelta(days=1))
        stem = file_stem or f"{identity}-{usage.dotted_string}"
        cert_file, key_file = self.dir / f"{stem}.pem", self.dir / f"{stem}.key"
        cert_file.write_bytes(cert)
        key_file.write_bytes(key)
        return cert_file, key_file

    def server_ctx(self, identity=SERVICE_IDENTITY, usage=SERVER) -> ssl.SSLContext:
        return server_context(self.ca_file, *self.leaf(identity, usage))

    def client_ctx(self, identity=ORCHESTRATOR_IDENTITY, usage=CLIENT, trust: "TestCA" = None) -> ssl.SSLContext:
        return client_context((trust or self).ca_file, *self.leaf(identity, usage))


# ------------------------------------------------------------------ gateway inputs
def account(cash=D(100000)):
    return AccountSnapshot.build(as_of=T0, cash=cash, positions={}, open_orders=[], realized_pnl_today=D(0),
                                 orders_today=0, equity_high_water=cash)


def broker_for(a):
    return BrokerAccountView(source_id="sim", as_of=T0, positions={s: p.quantity for s, p in a.positions.items()},
                             open_order_count=len(a.open_orders))


def proposal(m, a, symbol="AAA", side=Side.BUY, limit=D("100.01"), notional=D(5000), pid="p1"):
    return OrderProposal(decision=Decision.TRADE, symbol=symbol, side=side, order_type=OrderType.LIMIT,
                         limit_price=limit, requested_notional=notional, invalidation_conditions=["x"],
                         evidence_refs=[f"quote:{symbol}"], public_rationale="r", proposal_id=pid,
                         market_snapshot_id=m.snapshot_id, account_snapshot_id=a.snapshot_id,
                         model_version="m", prompt_version="p", prompt_sha256="0" * 64, strategy_version="s")


def critic(pid="p1"):
    return CriticVerdict(verdict=CriticVerdictKind.PASS, max_notional=None, reason_codes=[], evidence_refs=[],
                         proposal_id=pid, model_version="c", prompt_version="c")


def inputs(**pkw):
    m, a = market(), account()
    p = proposal(m, a, **pkw)
    return p, critic(p.proposal_id), m, a, broker_for(a)


# ------------------------------------------------------------------ service
def make_service(tmp_path, clock=None, kill=(), limiter=None, policy=POLICY, mode=Mode.BACKTEST,
                 signer=None, service_id="rs-test"):
    return build_risk_service(policy, mode, signer or EphemeralSigner(service_id), clock or SimulatedClock(T0),
                              CONFIG_DIR, Path(tmp_path) / "requests.jsonl", service_id, kill_switch_paths=kill,
                              limiter=limiter)


def records(tmp_path):
    log, _ = ChainedLog.open(Path(tmp_path) / "requests.jsonl", RequestRecord)
    return log.items


def wait_for(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def start(service, ca: TestCA, **kw) -> RiskServiceServer:
    return RiskServiceServer(service, ca.server_ctx(**kw), "127.0.0.1", 0).start()


def connect(server: RiskServiceServer, context: ssl.SSLContext, clock=None, approved=(POLICY.source_sha256,),
            mode=Mode.BACKTEST, timeout_s=3.0) -> RemoteRiskGateway:
    return RemoteRiskGateway(mode, approved, IdFactory("orch"), clock or SimulatedClock(T0),
                             port=server.address[1], context=context, timeout_s=timeout_s)


def local_exchange(service, mutate=None):
    """An Exchange that calls the service in-process (no network), optionally rewriting its JSON answer."""
    def exchange(method, path, body):
        if path.endswith("/info"):
            return service.handle_info(ORCHESTRATOR_IDENTITY)
        status, out = service.handle_evaluate(body, ORCHESTRATOR_IDENTITY)
        if mutate is not None and status == 200:
            doc = json.loads(out)
            mutate(doc)
            out = json.dumps(doc).encode("utf-8")
        return status, out
    return exchange
