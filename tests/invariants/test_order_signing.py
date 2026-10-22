"""ADR 0003 migration step 1: ApprovedOrder v2 (Ed25519) and the public-key-only OrderVerifier.

Acceptance tests 1 (signature binds every field), 2 (verifier holds public keys only), 3 (each refusal),
5 (key rotation), plus the vault's Signer and the pipeline halting on a refused order.
"""
import secrets
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading_agent.contracts.execution import ApprovedOrder
from trading_agent.contracts.audit import AuditEventType
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import DuplicateOrder, OrderRefused
from trading_agent.core.mode import Mode
from trading_agent.core.signing import (BACKTEST_ACCOUNT_ALIAS, EphemeralSigner, OrderTiming, TrustedKey,
                                        order_message)
from trading_agent.core.trading_state import TradingState
from trading_agent.core.types import IdFactory, Side
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.simulated_broker import SimulatedBroker
from trading_agent.execution.verifier import OrderVerifier
from trading_agent.risk.gateway import RiskGateway
from trading_agent.secret_store.vault import Vault, VaultSigner
from trading_agent.testing.harness import PASS_JSON, T0, Harness, market, risk_policy, trade_json

POLICY = risk_policy()


def signed(signer, **overrides) -> ApprovedOrder:
    fields = dict(order_id="o1", decision_id="d1", proposal_id="p1", symbol="AAA", side=Side.BUY,
                  limit_price=Decimal("100.01"), notional=Decimal("5000"), issued_at=T0,
                  expires_at=T0 + timedelta(seconds=5), environment=Mode.BACKTEST,
                  broker_account_alias=BACKTEST_ACCOUNT_ALIAS, leader_epoch=0, policy_version=POLICY.version,
                  policy_sha256=POLICY.source_sha256, market_snapshot_id="m1", account_snapshot_id="a1",
                  key_id=signer.key_id, signature="")
    fields.update(overrides)
    unsigned = ApprovedOrder(**fields)
    return unsigned.model_copy(update={"signature": signer.sign(order_message(unsigned.signing_payload())).hex()})


def verifier(*keys, clock=None, epoch=0, policies=(POLICY.source_sha256,)):
    return OrderVerifier(keys, Mode.BACKTEST, BACKTEST_ACCOUNT_ALIAS, policies, clock or SimulatedClock(T0),
                         leader_epoch=epoch)


def refusal(v, order) -> str:
    with pytest.raises(OrderRefused) as info:
        v.verify(order)
    return info.value.reason_code


@pytest.fixture()
def signer():
    return EphemeralSigner()


# ------------------------------------------------------------------ 1. signature binds every field
def test_gateway_order_is_v2_and_verifies(signer):
    clock = SimulatedClock(T0)
    gw = RiskGateway(POLICY, Mode.BACKTEST, signer, IdFactory("s"), clock)
    from test_risk_invariants import account, broker_for, critic, proposal
    m, a = market(), account()
    p = proposal(m, a)
    _, order = gw.evaluate(p, critic(p.proposal_id), m, a, TradingState.NORMAL, None, broker_for(a))
    assert order is not None and order.order_version == 2 and order.alg == "Ed25519"
    assert order.expires_at - order.issued_at == timedelta(seconds=5)
    assert (order.environment, order.broker_account_alias, order.leader_epoch) == (Mode.BACKTEST, "backtest-sim", 0)
    assert (order.policy_sha256, order.market_snapshot_id, order.account_snapshot_id) == \
        (POLICY.source_sha256, m.snapshot_id, a.snapshot_id)
    assert order.key_id == signer.key_id and len(order.signature) == 128
    verifier(signer.trusted_key(), clock=clock).verify(order)


def _changed(name, value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, Decimal):
        return value + 1
    if isinstance(value, datetime):
        return value - timedelta(milliseconds=1)
    if isinstance(value, Side):
        return Side.SELL if value is Side.BUY else Side.BUY
    if isinstance(value, Mode):
        return Mode.PAPER
    if name == "policy_sha256":
        return "f" * 64
    return value + "x"


def test_any_single_changed_field_fails(signer):
    order = signed(signer)
    v = verifier(signer.trusted_key())
    v.verify(order)
    for name in ApprovedOrder.model_fields:
        if name in ("signature", "order_version", "alg"):
            continue
        tampered = order.model_copy(update={name: _changed(name, getattr(order, name))})
        assert refusal(v, tampered) in ("BAD_SIGNATURE", "UNKNOWN_KEY"), name
    assert refusal(v, order.model_copy(update={"signature": "00" * 64})) == "BAD_SIGNATURE"
    assert refusal(v, order.model_copy(update={"signature": "not-hex"})) == "BAD_SIGNATURE"


def test_same_key_id_with_a_different_key_fails(signer):
    impostor = EphemeralSigner(key_id=signer.key_id)
    assert refusal(verifier(signer.trusted_key()), signed(impostor)) == "BAD_SIGNATURE"


# ------------------------------------------------------------------ 2. public keys only
def test_verifier_refuses_anything_that_can_sign(signer):
    for bad in (signer, Ed25519PrivateKey.generate(), signer.public_key()):
        with pytest.raises(TypeError):
            OrderVerifier([bad], Mode.BACKTEST, BACKTEST_ACCOUNT_ALIAS, [], SimulatedClock(T0))
    with pytest.raises(ValidationError):   # 64 bytes = seed + public key, i.e. a private key encoding
        TrustedKey(key_id="k", public_key=secrets.token_bytes(64))
    with pytest.raises(ValueError):
        OrderVerifier([], Mode.BACKTEST, BACKTEST_ACCOUNT_ALIAS, [], SimulatedClock(T0))
    with pytest.raises(TypeError):         # the execution port takes a verifier, never a signer
        SimulatedBroker(signer, CostModel(), lambda s: None, SimulatedClock(T0), IdFactory("b"))
    assert not any(callable(getattr(OrderVerifier, n, None)) for n in ("sign", "mint"))


def test_gateway_needs_a_signer():
    with pytest.raises(TypeError):
        RiskGateway(POLICY, Mode.BACKTEST, object(), IdFactory("s"), SimulatedClock(T0))


# ------------------------------------------------------------------ 3. refusals, each on its own
def test_unknown_key(signer):
    assert refusal(verifier(EphemeralSigner("other").trusted_key()), signed(signer)) == "UNKNOWN_KEY"


def test_revoked_key(signer):
    revoked = signer.trusted_key().model_copy(update={"revoked": True})
    assert refusal(verifier(revoked), signed(signer)) == "KEY_REVOKED"


def test_expired_order(signer):
    clock = SimulatedClock(T0)
    v = verifier(signer.trusted_key(), clock=clock)
    order = signed(signer)
    clock.advance_to(T0 + timedelta(seconds=5))
    v.verify(order)                                        # the last valid instant
    clock.advance_to(T0 + timedelta(seconds=5, microseconds=1))
    assert refusal(v, order) == "EXPIRED"


def test_order_from_the_future(signer):
    v = verifier(signer.trusted_key())                     # clock at T0, skew 1 s
    v.verify(signed(signer, order_id="o-skew", issued_at=T0 + timedelta(seconds=1),
                    expires_at=T0 + timedelta(seconds=6)))
    assert refusal(v, signed(signer, issued_at=T0 + timedelta(seconds=1, microseconds=1),
                             expires_at=T0 + timedelta(seconds=6))) == "FROM_THE_FUTURE"


def test_lifetime_longer_than_the_hard_maximum(signer):
    order = signed(signer, expires_at=T0 + timedelta(seconds=11))
    assert refusal(verifier(signer.trusted_key()), order) == "BAD_LIFETIME"
    assert refusal(verifier(signer.trusted_key()), signed(signer, expires_at=T0)) == "BAD_LIFETIME"


def test_wrong_environment(signer):
    assert refusal(verifier(signer.trusted_key()), signed(signer, environment=Mode.PAPER)) == "WRONG_ENVIRONMENT"


def test_wrong_account(signer):
    assert refusal(verifier(signer.trusted_key()), signed(signer, broker_account_alias="other")) == "WRONG_ACCOUNT"


def test_stale_epoch(signer):
    epoch = {"now": 3}
    v = verifier(signer.trusted_key(), epoch=lambda: epoch["now"])
    order = signed(signer, leader_epoch=3)
    v.verify(order)
    epoch["now"] = 4                                       # a new leader took over
    assert refusal(v, order) == "STALE_EPOCH"


def test_unapproved_policy(signer):
    assert refusal(verifier(signer.trusted_key(), policies=["a" * 64]), signed(signer)) == "UNAPPROVED_POLICY"


def test_unknown_algorithm(signer):
    assert refusal(verifier(signer.trusted_key()), signed(signer).model_copy(update={"alg": "HS256"})) == \
        "UNKNOWN_ALG"


def test_replayed_order_id(signer):
    clock = SimulatedClock(T0)
    m = market()
    broker = SimulatedBroker(verifier(signer.trusted_key(), clock=clock), CostModel(), lambda s: m.quotes.get(s),
                             clock, IdFactory("b"))
    order = signed(signer)
    broker.submit(order)
    with pytest.raises(DuplicateOrder):
        broker.submit(order)


def test_timing_bounds():
    assert OrderTiming() == OrderTiming(ttl_seconds=5, clock_skew_seconds=1)
    for bad in (dict(ttl_seconds=11), dict(ttl_seconds=0), dict(clock_skew_seconds=2)):
        with pytest.raises(ValidationError):
            OrderTiming(**bad)


# ------------------------------------------------------------------ 5. rotation
def test_rotation_overlap_then_old_key_retires():
    old, new = EphemeralSigner("risk-1"), EphemeralSigner("risk-2")
    cutover = T0 + timedelta(hours=1)
    keys = (old.trusted_key(not_before=T0 - timedelta(days=1), not_after=cutover),
            new.trusted_key(not_before=T0 - timedelta(minutes=5)))
    clock = SimulatedClock(T0)
    v = verifier(*keys, clock=clock)
    v.verify(signed(old))                                  # overlap: both verify
    v.verify(signed(new, order_id="o2"))
    late = cutover + timedelta(seconds=1)
    clock.advance_to(late)
    assert refusal(v, signed(old, order_id="o3", issued_at=late, expires_at=late + timedelta(seconds=5))) == \
        "KEY_NOT_VALID_AT_ISSUE"
    v.verify(signed(new, order_id="o4", issued_at=late, expires_at=late + timedelta(seconds=5)))


# ------------------------------------------------------------------ vault Signer port
def test_vault_signer_signs_orders_the_verifier_accepts(tmp_path):
    clock = SimulatedClock(T0)
    vault = Vault.create(tmp_path / "t.vault", "pw-" + secrets.token_hex(8), tmp_path / "log.jsonl", clock=clock,
                         scrypt_n=2 ** 12, insecure_fast_kdf_for_tests=True)
    pub = vault.create_signing_key("risk-signing-1", T0 - timedelta(hours=1), T0 + timedelta(days=1))
    client = vault.as_client(vault.grant("risk-service", sign=["risk-signing-1"]))
    order = signed(VaultSigner(client, "risk-signing-1"))
    key = TrustedKey(key_id="risk-signing-1", public_key=pub, not_before=T0 - timedelta(hours=1),
                     not_after=T0 + timedelta(days=1))
    verifier(key, clock=clock).verify(order)


# ------------------------------------------------------------------ pipeline: refusal halts, audited
def test_refused_order_halts_the_pipeline_with_its_reason():
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    h.broker._verifier = verifier(EphemeralSigner("other").trusted_key(), clock=h.clock)  # trusts another key
    result = h.tick()
    assert result.order is not None and result.execution is None
    assert h.state.state is TradingState.HALTED
    reasons = [r.details.get("reason") for r in h.ledger.records if r.event_type is AuditEventType.STATE_CHANGE]
    assert reasons == ["OrderRefused:UNKNOWN_KEY"]
    assert h.broker.broker_positions() == {}
