"""ADR 0003 step 3: the risk service in its own process behind mutual TLS.

Covers: the signed order made by the service verifies at the execution side and matches the in-process
gateway; mutual TLS refuses every wrong peer on both sides; loopback only; the request schema is closed;
replay, rate limit, kill switch and request-log failure never yield an order; the client turns every fault
into a DENY; the live loop runs end to end over the link; the entry point runs as a separate OS process.
"""
import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from rs_helpers import (CLIENT, CONFIG_DIR, POLICY, SERVER, TestCA, connect, inputs, local_exchange, make_service,
                        records, start, wait_for)
from trading_agent.contracts.risk import RiskVerdict
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import ConfigError, ModeNotPermitted
from trading_agent.core.mode import Mode
from trading_agent.core.signing import EphemeralSigner, TrustedKey
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import IdFactory
from trading_agent.execution.verifier import OrderVerifier
from trading_agent.risk.gateway import RiskGateway
from trading_agent.risk_service import service as service_module
from trading_agent.risk_service.client import RemoteRiskGateway
from trading_agent.risk_service.pki import generate_dev_pki
from trading_agent.risk_service.protocol import EVALUATE_PATH, ORCHESTRATOR_IDENTITY, EvaluateRequest, encode
from trading_agent.risk_service.server import RiskServiceServer
from trading_agent.risk_service.service import TokenBucket
from trading_agent.risk_service.tls import client_context, server_context
from trading_agent.testing.harness import ROOT, T0, market, risk_policy

pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")


@pytest.fixture()
def ca(tmp_path):
    return TestCA(tmp_path / "pki")


@pytest.fixture()
def running(tmp_path, ca):
    clock = SimulatedClock(T0)
    service = make_service(tmp_path, clock)
    server = start(service, ca)
    yield service, server, clock
    server.stop()


def request_body(rid="r1", **pkw):
    p, c, m, a, b = inputs(**pkw)
    return encode(EvaluateRequest(request_id=rid, proposal=p, critic=c, market=m, account=a,
                                  trading_state=TradingState.NORMAL, broker=b))


def answer(status_body):
    status, body = status_body
    return status, json.loads(body)


# ------------------------------------------------------------------ round trip
def test_an_order_signed_by_the_service_verifies_at_the_execution_side(running, ca):
    service, server, clock = running
    gw = connect(server, ca.client_ctx(), clock)
    p, c, m, a, b = inputs()
    decision, order = gw.evaluate(p, c, m, a, TradingState.NORMAL, None, b)
    assert decision.verdict in (RiskVerdict.APPROVE, RiskVerdict.REDUCE) and order is not None
    assert decision.decision_id.startswith("rs-test:risk:") and order.order_id.startswith("rs-test:order:")
    verifier = OrderVerifier([TrustedKey(key_id=gw.key_id, public_key=gw.public_key)], Mode.BACKTEST,
                             gw.account_alias, {POLICY.source_sha256}, clock)
    verifier.verify(order)
    assert gw.policy.version == POLICY.version and gw.policy.summary() == POLICY.summary()


@pytest.mark.parametrize("pkw, state", [
    ({}, TradingState.NORMAL),
    ({"notional": Decimal(900000)}, TradingState.NORMAL),          # capped => REDUCE
    ({"symbol": "ZZZ"}, TradingState.NORMAL),                      # not in the universe => DENY
    ({}, TradingState.HALTED),                                     # halted => DENY
    ({"limit": Decimal("120.00")}, TradingState.NORMAL),           # outside the price collar => DENY
])
def test_the_service_decides_exactly_like_the_in_process_gateway(tmp_path, pkw, state):
    service = make_service(tmp_path)
    remote = RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0),
                               exchange=local_exchange(service))
    local = RiskGateway(POLICY, Mode.BACKTEST, EphemeralSigner(), IdFactory("l"), SimulatedClock(T0))
    p, c, m, a, b = inputs(**pkw)
    r_dec, r_order = remote.evaluate(p, c, m, a, state, None, b)
    l_dec, l_order = local.evaluate(p, c, m, a, state, None, b)
    assert (r_dec.verdict, r_dec.approved_notional) == (l_dec.verdict, l_dec.approved_notional)
    assert sorted(r_dec.reason_codes) == sorted(l_dec.reason_codes)
    assert (r_order is None) == (l_order is None)
    if r_order:
        assert (r_order.symbol, r_order.side, r_order.notional, r_order.limit_price) == \
            (l_order.symbol, l_order.side, l_order.notional, l_order.limit_price)


# ------------------------------------------------------------------ mutual TLS
def _refused_at_start(server, context):
    with pytest.raises(ConfigError):
        connect(server, context)


def test_a_client_without_a_certificate_is_refused_and_logged(tmp_path, running, ca):
    _, server, _ = running
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=str(ca.ca_file))
    _refused_at_start(server, ctx)
    assert wait_for(lambda: any(r.reason_code == "TLS_HANDSHAKE_FAILED" for r in records(tmp_path)))


def test_a_client_certificate_from_another_ca_is_refused(tmp_path, running, ca):
    _, server, _ = running
    other = TestCA(tmp_path / "other", "attacker CA")
    _refused_at_start(server, other.client_ctx(trust=ca))


def test_a_client_presenting_the_server_certificate_is_refused(running, ca):
    _, server, _ = running                                      # EKU serverAuth only: not a client certificate
    _refused_at_start(server, ca.client_ctx(identity="risk-service", usage=SERVER))


def test_a_valid_client_certificate_for_another_identity_is_refused(tmp_path, running, ca):
    _, server, _ = running
    _refused_at_start(server, ca.client_ctx(identity="planner", usage=CLIENT))
    assert any(r.reason_code == "PEER_NOT_ALLOWED" and r.peer == "planner" for r in records(tmp_path))


@pytest.mark.parametrize("identity, usage", [("orchestrator", CLIENT), ("other-service", SERVER)])
def test_the_client_refuses_a_server_that_is_not_the_risk_service(tmp_path, ca, identity, usage):
    service = make_service(tmp_path)
    with RiskServiceServer(service, server_context(ca.ca_file, *ca.leaf(identity, usage)), "127.0.0.1", 0) as srv:
        _refused_at_start(srv, ca.client_ctx())


def test_plain_http_and_tls_1_2_get_no_answer(running, ca):
    _, server, _ = running
    conn = http.client.HTTPConnection("127.0.0.1", server.address[1], timeout=3)
    with pytest.raises((http.client.HTTPException, OSError)):
        conn.request("GET", "/v1/info")
        conn.getresponse()
    ctx = ca.client_ctx()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    _refused_at_start(server, ctx)


def test_the_link_is_loopback_only_and_needs_an_authenticating_context(tmp_path, ca):
    service = make_service(tmp_path)
    for host in ("0.0.0.0", "10.0.0.5", "risk.example.com", ""):
        with pytest.raises(ConfigError):
            RiskServiceServer(service, ca.server_ctx(), host, 0)
        with pytest.raises(ConfigError):
            RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0),
                              host=host, port=1, context=ca.client_ctx())
    lax = ca.client_ctx()
    lax.check_hostname = False
    with pytest.raises(ConfigError):
        RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0), port=1,
                          context=lax)
    with pytest.raises(ConfigError):
        RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0), port=1)


def test_a_stalled_peer_does_not_block_the_service(running, ca):
    _, server, clock = running
    stalled = socket.create_connection(server.address, timeout=10)   # connects, never says anything
    try:
        t0 = time.monotonic()
        gw = connect(server, ca.client_ctx(), clock, timeout_s=10)
        p, c, m, a, b = inputs()
        assert gw.evaluate(p, c, m, a, TradingState.NORMAL, None, b).order is not None
        assert time.monotonic() - t0 < 9
    finally:
        stalled.close()


# ------------------------------------------------------------------ closed schema, replay, limits
@pytest.mark.parametrize("extra", [{"mode": "PAPER"}, {"policy": {"max_order_notional": "1e9"}},
                                   {"account_alias": "live-1"}, {"signature": "00"}])
def test_a_request_cannot_carry_mode_policy_account_or_signature(tmp_path, extra):
    service = make_service(tmp_path)
    doc = json.loads(request_body())
    doc.update(extra)
    status, out = answer(service.handle_evaluate(json.dumps(doc).encode(), ORCHESTRATOR_IDENTITY))
    assert (status, out["reason_code"]) == (400, "SCHEMA_FAILURE") and "order" not in out


@pytest.mark.parametrize("body, code", [
    (b'{"request_id": "a", "request_id": "b"}', "BAD_JSON"),
    (b'{"x": NaN}', "BAD_JSON"),
    (b'[1, 2]', "BAD_JSON"),
    (b'\xff\xfe', "BAD_JSON"),
    (b'{}', "SCHEMA_FAILURE"),
    (None, "BODY_TOO_LARGE"),
], ids=["duplicate-key", "nan", "array", "not-utf8", "empty-object", "too-large"])
def test_malformed_bodies_are_refused(tmp_path, body, code):
    body = body if body is not None else b'{"x": "' + b"a" * (600 * 1024) + b'"}'
    status, out = answer(make_service(tmp_path).handle_evaluate(body, ORCHESTRATOR_IDENTITY))
    assert out["reason_code"] == code and status >= 400


def test_a_replayed_request_never_yields_a_second_order(tmp_path):
    service = make_service(tmp_path)
    body = request_body("same")
    first = answer(service.handle_evaluate(body, ORCHESTRATOR_IDENTITY))
    second = answer(service.handle_evaluate(body, ORCHESTRATOR_IDENTITY))
    assert first[0] == 200 and first[1]["order"] is not None
    assert second == (409, {"protocol_version": 1, "request_id": "same", "reason_code": "DUPLICATE_REQUEST"})


def test_only_the_orchestrator_identity_is_answered(tmp_path):
    service = make_service(tmp_path)
    for peer in ("", "planner", "risk-service", "Orchestrator"):
        assert answer(service.handle_evaluate(request_body(peer or "x"), peer))[1]["reason_code"] == \
            "PEER_NOT_ALLOWED"
        assert answer(service.handle_info(peer))[0] == 403


def test_the_rate_limit_refuses_bursts(tmp_path):
    now = [0.0]
    service = make_service(tmp_path, limiter=TokenBucket(rate_per_s=1, burst=2, monotonic=lambda: now[0]))
    codes = [answer(service.handle_evaluate(request_body(f"r{i}"), ORCHESTRATOR_IDENTITY))[0] for i in range(3)]
    assert codes == [200, 200, 429]
    now[0] += 1.0
    assert answer(service.handle_evaluate(request_body("r9"), ORCHESTRATOR_IDENTITY))[0] == 200


def test_the_kill_switch_stops_signing(tmp_path):
    switch = tmp_path / "KILL_SWITCH"
    service = make_service(tmp_path, kill=[switch])
    assert answer(service.handle_evaluate(request_body("a"), ORCHESTRATOR_IDENTITY))[1]["order"] is not None
    switch.write_text("stop", encoding="utf-8")
    status, out = answer(service.handle_evaluate(request_body("b"), ORCHESTRATOR_IDENTITY))
    assert status == 200 and out["order"] is None and out["decision"]["verdict"] == "DENY"
    assert "KILL_SWITCH_ACTIVE" in [r["reason_code"] for r in out["decision"]["rule_results"]]


def test_an_unreadable_kill_switch_counts_as_active(tmp_path, monkeypatch):
    service = make_service(tmp_path, kill=[tmp_path / "KILL_SWITCH"])

    def broken_stat(path, *a, **kw):
        raise PermissionError("denied")
    monkeypatch.setattr(service_module, "_stat", broken_stat)
    status, out = answer(service.handle_evaluate(request_body(), ORCHESTRATOR_IDENTITY))
    assert out["order"] is None
    assert "KILL_SWITCH_UNREADABLE" in [r["reason_code"] for r in out["decision"]["rule_results"]]


# ------------------------------------------------------------------ request log
def test_every_request_is_logged_in_a_chain_without_signatures_or_keys(tmp_path):
    service = make_service(tmp_path)
    ok = answer(service.handle_evaluate(request_body("a"), ORCHESTRATOR_IDENTITY))[1]
    service.handle_evaluate(b"{}", ORCHESTRATOR_IDENTITY)
    service.handle_evaluate(request_body("b"), "planner")
    service.handle_info(ORCHESTRATOR_IDENTITY)
    rows = records(tmp_path)                                   # reopening verifies the hash chain
    assert [(r.endpoint, r.outcome, r.reason_code) for r in rows] == [
        ("evaluate", "ANSWERED", None), ("evaluate", "REFUSED", "SCHEMA_FAILURE"),
        ("evaluate", "REFUSED", "PEER_NOT_ALLOWED"), ("info", "ANSWERED", None)]
    assert rows[0].order_id == ok["order"]["order_id"] and rows[0].request_sha256
    text = (tmp_path / "requests.jsonl").read_text(encoding="utf-8")
    assert ok["order"]["signature"] not in text and "PRIVATE" not in text


def test_a_failed_log_write_discards_the_order(tmp_path):
    service = make_service(tmp_path)
    service._log.fail_writes = True
    status, out = answer(service.handle_evaluate(request_body(), ORCHESTRATOR_IDENTITY))
    assert (status, out["reason_code"]) == (503, "AUDIT_WRITE_FAILURE") and "order" not in out


# ------------------------------------------------------------------ start-up refusals
def test_the_service_refuses_to_start_unapproved_or_in_a_forbidden_mode(tmp_path):
    with pytest.raises(ConfigError):
        make_service(tmp_path, policy=risk_policy(source_sha256="f" * 64))
    with pytest.raises(ModeNotPermitted):
        make_service(tmp_path, mode=Mode.PAPER)


def test_the_info_endpoint_publishes_only_the_public_key(tmp_path):
    signer = EphemeralSigner("rs-test")
    service = make_service(tmp_path, signer=signer)
    status, info = answer(service.handle_info(ORCHESTRATOR_IDENTITY))
    assert status == 200 and info["public_key_hex"] == signer.public_key().hex()
    assert set(info) == {"protocol_version", "service_id", "environment", "account_alias", "policy_version",
                         "policy_sha256", "policy_summary", "key_id", "alg", "public_key_hex", "order_ttl_seconds"}


# ------------------------------------------------------------------ client fails closed
def _client(service, mutate=None, **kw):
    return RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0),
                             exchange=local_exchange(service, mutate), **kw)


def _deny_code(gw, **pkw):
    p, c, m, a, b = inputs(**pkw)
    decision, order = gw.evaluate(p, c, m, a, TradingState.NORMAL, None, b)
    assert order is None and decision.verdict is RiskVerdict.DENY and decision.approved_notional == 0
    return decision.reason_codes[0]


def test_the_client_refuses_to_start_against_the_wrong_mode_or_policy(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ConfigError):
        RemoteRiskGateway(Mode.PAPER, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0),
                          exchange=local_exchange(service))
    with pytest.raises(ConfigError):
        RemoteRiskGateway(Mode.BACKTEST, {"e" * 64}, IdFactory("o"), SimulatedClock(T0),
                          exchange=local_exchange(service))

    def down(*a):
        raise ConnectionRefusedError()
    with pytest.raises(ConfigError):
        RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0), exchange=down)


def test_transport_faults_become_deny(tmp_path):
    service = make_service(tmp_path)
    gw = _client(service)
    faults = {"down": ConnectionRefusedError(), "timeout": socket.timeout(), "tls": ssl.SSLError(),
              "http": http.client.RemoteDisconnected(), "bug": RuntimeError()}
    for name, exc in faults.items():
        def broken(method, path, body, exc=exc):
            raise exc
        gw._exchange = broken
        assert _deny_code(gw) == ("RISK_SERVICE_ERROR" if name == "bug" else "RISK_SERVICE_UNAVAILABLE")
    gw._exchange = lambda *a: (429, b'{"protocol_version":1,"request_id":null,"reason_code":"RATE_LIMITED"}')
    assert _deny_code(gw) == "RISK_SERVICE_RATE_LIMITED"
    gw._exchange = lambda *a: (500, b"<html>oops</html>")
    assert _deny_code(gw) == "RISK_SERVICE_ERROR"
    gw._exchange = lambda *a: (200, b'{"protocol_version": 1, "request_id": "x"')
    assert _deny_code(gw) == "RISK_SERVICE_BAD_RESPONSE"


def _set(path, value):
    def mutate(doc):
        node = doc
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value(node[path[-1]]) if callable(value) else value
    return mutate


@pytest.mark.parametrize("mutate", [
    _set(("request_id",), "someone-else"),
    _set(("decision", "proposal_id"), "p999"),
    _set(("decision", "policy_sha256"), "a" * 64),
    _set(("order", "symbol"), "BBB"),
    _set(("order", "side"), "SELL"),
    _set(("order", "notional"), lambda v: str(Decimal(v) + 1)),
    _set(("order", "limit_price"), "100.50"),
    _set(("order", "environment"), "PAPER"),
    _set(("order", "broker_account_alias"), "live-1"),
    _set(("order", "key_id"), "attacker"),
    _set(("order", "market_snapshot_id"), "m-other"),
    _set(("order", "decision_id"), "d-other"),
    _set(("order",), None),
], ids=lambda f: "mutation")
def test_an_answer_that_does_not_match_the_request_yields_no_order(tmp_path, mutate):
    gw = _client(make_service(tmp_path), mutate)
    assert _deny_code(gw) in ("RISK_SERVICE_INCONSISTENT_RESPONSE", "RISK_SERVICE_BAD_RESPONSE")


def test_an_order_attached_to_a_deny_is_dropped(tmp_path):
    service = make_service(tmp_path)
    good = json.loads(service.handle_evaluate(request_body("x"), ORCHESTRATOR_IDENTITY)[1])

    def add_order(doc):
        doc["order"] = good["order"]
    gw = _client(service, add_order)
    assert _deny_code(gw, symbol="ZZZ") == "RISK_SERVICE_INCONSISTENT_RESPONSE"


def test_a_service_that_dies_mid_run_means_deny(tmp_path, ca):
    service = make_service(tmp_path)
    server = start(service, ca)
    gw = connect(server, ca.client_ctx())
    server.stop()
    assert _deny_code(gw) == "RISK_SERVICE_UNAVAILABLE"


# ------------------------------------------------------------------ live loop over the link
def test_the_live_loop_runs_over_the_mtls_link(tmp_path, ca):
    from trading_agent.orchestration.session_metrics import load_and_compute
    from trading_agent.testing.live import ManualClock, at, drive, make_runner

    clock = ManualClock(at("18:50"))
    service = make_service(tmp_path, clock)
    with start(service, ca) as server:
        gw = connect(server, ca.client_ctx(), clock)
        runner, replay = make_runner(tmp_path / "run", clock, risk_gateway=gw)
        assert runner.gateway is gw
        drive(runner, replay, clock, 30)
    m = load_and_compute(tmp_path / "run", "2026-01-13")
    assert m.decisions == 30 and m.simulated_fills >= 1 and runner.state.state is TradingState.NORMAL
    answered = [r for r in records(tmp_path) if r.endpoint == "evaluate" and r.outcome == "ANSWERED"]
    assert len(answered) == 30 and any(r.order_id for r in answered)
    assert "signature" not in (tmp_path / "run" / "packets.jsonl").read_text(encoding="utf-8")


def test_the_live_loop_refuses_a_risk_service_with_another_mode_or_policy(tmp_path):
    from trading_agent.testing.live import ManualClock, at, make_runner

    class Other:
        mode = Mode.BACKTEST
        policy = type("P", (), {"source_sha256": "b" * 64})()
    with pytest.raises(ConfigError):
        make_runner(tmp_path / "run", ManualClock(at("18:50")), risk_gateway=Other())


# ------------------------------------------------------------------ development PKI and the separate process
def test_dev_pki_works_end_to_end_and_never_writes_the_ca_key(tmp_path):
    files = generate_dev_pki(tmp_path / "pki")
    assert sorted(p.name for p in (tmp_path / "pki").iterdir()) == \
        ["ca.pem", "orchestrator.key", "orchestrator.pem", "risk-service.key", "risk-service.pem"]
    keys = [p.name for p in (tmp_path / "pki").iterdir() if b"PRIVATE KEY" in p.read_bytes()]
    assert sorted(keys) == ["orchestrator.key", "risk-service.key"]
    service = make_service(tmp_path)
    ctx = server_context(files["ca"], files["risk-service.pem"], files["risk-service.key"])
    with RiskServiceServer(service, ctx, "127.0.0.1", 0) as server:
        gw = connect(server, client_context(files["ca"], files["orchestrator.pem"], files["orchestrator.key"]))
        p, c, m, a, b = inputs()
        assert gw.evaluate(p, c, m, a, TradingState.NORMAL, None, b).order is not None
    with pytest.raises(ConfigError):
        generate_dev_pki(tmp_path / "pki")                     # never overwrites: rotate into a new directory
    with pytest.raises(ConfigError):
        generate_dev_pki(tmp_path / "x", days=365)


def test_keys_are_never_written_inside_the_repository(tmp_path):
    target = ROOT / "runs" / "pki-should-not-exist"
    with pytest.raises(ConfigError):
        generate_dev_pki(target)
    assert not target.exists()
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_dev_certs.py"), str(target)],
                          capture_output=True, text=True, timeout=60, env=_env())
    assert proc.returncode == 2 and not target.exists()
    from trading_agent.risk_service.__main__ import main
    assert main(["--pki-dir", str(tmp_path), "--log", str(ROOT / "runs" / "requests.jsonl")]) == 2


def _env():
    paths = [str(ROOT / "src"), str(ROOT / ".deps")] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH")
                                                        else [])
    env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ("TOKEN", "KEY", "SECRET"))}
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def test_the_entry_point_runs_the_service_as_its_own_process(tmp_path):
    files = generate_dev_pki(tmp_path / "pki")
    proc = subprocess.Popen([sys.executable, "-m", "trading_agent.risk_service", "--pki-dir", str(tmp_path / "pki"),
                             "--log", str(tmp_path / "service" / "requests.jsonl"), "--port", "0",
                             "--kill-switch", str(tmp_path / "KILL_SWITCH")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env())
    try:
        line = proc.stdout.readline()
        assert line.startswith("risk service rs-") and "mode=BACKTEST" in line and "PRIVATE" not in line, \
            line + proc.stderr.read() if proc.poll() is not None else line
        port = int(line.split("https://127.0.0.1:", 1)[1].split()[0])
        gw = RemoteRiskGateway(Mode.BACKTEST, {POLICY.source_sha256}, IdFactory("o"), SimulatedClock(T0), port=port,
                               context=client_context(files["ca"], files["orchestrator.pem"],
                                                      files["orchestrator.key"]))
        assert gw.key_id.startswith("rs-") and gw.info.service_id == gw.key_id
        p, c, m, a, b = inputs()
        decision, _ = gw.evaluate(p, c, m, a, TradingState.NORMAL, None, b)
        assert decision.decision_id.startswith(gw.key_id + ":risk:")   # decided in the other process
    finally:
        proc.terminate()
        proc.wait(timeout=10)
