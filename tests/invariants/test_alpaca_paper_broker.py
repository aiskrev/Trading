"""Roadmap P6: the Alpaca PAPER execution port against a scripted fake of the Alpaca trading API.

No network, no real keys. Covers the ADR 0003 execution checks (verify first, idempotency before send,
client_order_id), IOC semantics, every ambiguous broker answer => BrokerStateUnknown (HALT) with the order left
RESERVED for recovery, and the request allow-list (no cancel-all, no close-all, no live host).
"""
import json
import secrets as _secrets
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr

from trading_agent.contracts.execution import AckStatus
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import DuplicateOrder, OrderRefused, SecretUnavailable, TransportError
from trading_agent.core.mode import Mode
from trading_agent.core.signing import BACKTEST_ACCOUNT_ALIAS, EphemeralSigner, order_message
from trading_agent.core.types import IdFactory, Side
from trading_agent.execution.alpaca_http import PAPER_HOST, AlpacaTradingClient, RawResponse
from trading_agent.execution.alpaca_paper import AlpacaPaperBroker, BrokerStateUnknown
from trading_agent.execution.idempotency import IdempotencyStore, Phase, client_order_id, resolve_unresolved
from trading_agent.contracts.execution import ApprovedOrder
from trading_agent.testing.harness import T0, risk_policy, verifier_for

POLICY = risk_policy()


class Secrets:
    def __init__(self):
        self.key, self.secret = "PK" + _secrets.token_hex(8).upper(), _secrets.token_urlsafe(24)

    def get(self, name):
        if name != "alpaca-paper":
            raise SecretUnavailable(name)
        return SecretStr(f"{self.key}:{self.secret}")


class FakeAlpaca:
    """Scripted paper API. `mode`: fill | partial | never_final | cancel_then_final | reject | no_fill."""

    def __init__(self, mode="fill", post_status=200, post_error=None, positions=None, echo=None):
        self.mode, self.post_status, self.post_error = mode, post_status, post_error
        self.positions = positions if positions is not None else []
        self.echo = echo or {}
        self.orders, self.calls = {}, []
        self.gets = 0
        self.positions_error = 0

    def request(self, method, path, headers, body, timeout_s):
        self.calls.append((method, path, dict(headers), body))
        if method == "POST" and path == "/v2/orders":
            if self.post_error:
                raise self.post_error
            if self.post_status != 200:
                return RawResponse(self.post_status, b'{"code":40310000,"message":"insufficient buying power"}')
            req = json.loads(body)
            oid = str(uuid.uuid4())
            order = {"id": oid, "client_order_id": req["client_order_id"], "symbol": req["symbol"],
                     "side": req["side"], "type": req["type"], "time_in_force": req["time_in_force"],
                     "status": "new", "qty": req["qty"], "filled_qty": "0", "filled_avg_price": None,
                     "limit_price": req["limit_price"], "asset_class": "us_equity",
                     "created_at": T0.isoformat(), "updated_at": T0.isoformat(), "filled_at": None}
            order.update(self.echo)
            self.orders[oid] = order
            return RawResponse(200, json.dumps(order).encode())
        if method == "GET" and path.startswith("/v2/orders/"):
            o = self.orders.get(path.rsplit("/", 1)[1])
            if o is None:
                return RawResponse(404, b'{"message":"order not found"}')
            self.gets += 1
            self._advance(o)
            return RawResponse(200, json.dumps(o).encode())
        if method == "DELETE" and path.startswith("/v2/orders/"):
            o = self.orders[path.rsplit("/", 1)[1]]
            if self.mode == "never_final":
                return RawResponse(204, b"")
            if self.mode == "cancel_then_final":
                o.update(status="canceled", filled_qty="2", filled_avg_price="100.00")
            return RawResponse(204, b"")
        if method == "GET" and path.startswith("/v2/orders:by_client_order_id"):
            cid = path.split("=", 1)[1]
            for o in self.orders.values():
                if o["client_order_id"] == cid:
                    return RawResponse(200, json.dumps(o).encode())
            return RawResponse(404, b'{"code":40410000,"message":"order not found"}')
        if method == "GET" and path == "/v2/positions":
            if self.positions_error:
                self.positions_error -= 1
                return RawResponse(503, b"")
            return RawResponse(200, json.dumps(self.positions).encode())
        if method == "GET" and path == "/v2/account":
            return RawResponse(200, json.dumps(self.account).encode())
        return RawResponse(404, b"")

    def _advance(self, o):
        if self.mode == "fill":
            o.update(status="filled", filled_qty=o["qty"], filled_avg_price="100.00", filled_at=T0.isoformat())
        elif self.mode == "partial":
            o.update(status="canceled", filled_qty="3", filled_avg_price="99.99")
        elif self.mode == "reject":
            o.update(status="rejected")
        elif self.mode == "no_fill":
            o.update(status="canceled")
        elif self.mode == "overfill":
            o.update(status="filled", filled_qty=str(int(o["qty"]) + 1), filled_avg_price="100.00")
        elif self.mode == "no_price":
            o.update(status="filled", filled_qty=o["qty"], filled_avg_price=None)

    account = {"id": "x", "account_number": "PA1", "status": "ACTIVE", "currency": "USD", "cash": "100000",
               "equity": "100000", "buying_power": "200000", "trading_blocked": False, "account_blocked": False}

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class Mono:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def setup(fake=None, idem=None, signer=None, wait=1.0):
    clock, mono, secrets = SimulatedClock(T0), Mono(), Secrets()
    signer = signer or EphemeralSigner()
    fake = fake or FakeAlpaca()
    client = AlpacaTradingClient(fake, secrets, "alpaca-paper", clock)
    broker = AlpacaPaperBroker(verifier_for(signer, clock), client, clock, IdFactory("t"), idempotency=idem,
                               fill_wait_s=wait, poll_s=0.25, sleep=mono.sleep, monotonic=mono)
    return broker, fake, signer, clock, secrets


def signed(signer, side=Side.BUY, notional="1000", limit="100.01", order_id="o1"):
    unsigned = ApprovedOrder(order_id=order_id, decision_id="d1", proposal_id="p1", symbol="AAA", side=side,
                             limit_price=Decimal(limit), notional=Decimal(notional), issued_at=T0,
                             expires_at=T0 + timedelta(seconds=5), environment=Mode.BACKTEST,
                             broker_account_alias=BACKTEST_ACCOUNT_ALIAS, leader_epoch=0,
                             policy_version=POLICY.version, policy_sha256=POLICY.source_sha256,
                             market_snapshot_id="m1", account_snapshot_id="a1", key_id=signer.key_id, signature="")
    return unsigned.model_copy(update={"signature": signer.sign(order_message(unsigned.signing_payload())).hex()})


# ------------------------------------------------------------------ happy path
def test_a_signed_order_becomes_one_ioc_limit_order_for_whole_shares(tmp_path):
    idem, _ = IdempotencyStore.open(tmp_path / "idem.jsonl")
    broker, fake, signer, clock, secrets = setup(idem=idem)
    order = signed(signer)
    result = broker.submit(order)
    (method, path, headers, body), = fake.posts()
    assert json.loads(body) == {"symbol": "AAA", "qty": "9", "side": "buy", "type": "limit", "time_in_force": "ioc",
                                "limit_price": "100.01", "client_order_id": client_order_id(order)}
    assert result.ack.status is AckStatus.ACCEPTED
    (fill,) = result.fills
    assert (fill.quantity, fill.price, fill.fee, fill.side) == (Decimal(9), Decimal("100.00"), Decimal(0), Side.BUY)
    assert fill.quantity * fill.price <= order.notional                     # never above the approved notional
    rec = idem.get(order.order_id)
    assert (rec.phase, rec.outcome) == (Phase.COMPLETED, "ACCEPTED")
    assert headers["APCA-API-KEY-ID"] == secrets.key and secrets.secret not in body.decode()


def test_partial_fill_and_no_fill_follow_ioc_semantics():
    broker, fake, signer, *_ = setup(FakeAlpaca("partial"))
    r = broker.submit(signed(signer))
    assert r.ack.status is AckStatus.ACCEPTED and r.fills[0].quantity == Decimal(3)
    broker, fake, signer, *_ = setup(FakeAlpaca("no_fill"))
    r = broker.submit(signed(signer))
    assert (r.ack.status, r.fills) == (AckStatus.EXPIRED, [])
    broker, fake, signer, *_ = setup(FakeAlpaca("reject"))
    assert broker.submit(signed(signer)).ack.status is AckStatus.REJECTED


def test_an_order_left_open_is_cancelled_then_settled():
    broker, fake, signer, *_ = setup(FakeAlpaca("cancel_then_final"))
    fake._advance = lambda o: None                    # stays "new" until cancelled
    r = broker.submit(signed(signer))
    assert [c[0] for c in fake.calls].count("DELETE") == 1
    assert r.ack.status is AckStatus.ACCEPTED and r.fills[0].quantity == Decimal(2)


def test_an_order_still_open_after_cancel_halts(tmp_path):
    idem, _ = IdempotencyStore.open(tmp_path / "idem.jsonl")
    broker, fake, signer, *_ = setup(FakeAlpaca("never_final"), idem=idem)
    fake._advance = lambda o: None
    order = signed(signer)
    with pytest.raises(BrokerStateUnknown):
        broker.submit(order)
    assert idem.get(order.order_id).phase is Phase.RESERVED


# ------------------------------------------------------------------ refusals before anything is sent
def test_a_forged_or_expired_order_is_refused_before_any_request():
    broker, fake, signer, clock, _ = setup()
    with pytest.raises(OrderRefused):
        broker.submit(signed(EphemeralSigner()))                 # another key
    clock.advance(10)
    with pytest.raises(OrderRefused):
        broker.submit(signed(signer))                            # expired
    assert fake.calls == []


def test_a_replayed_order_is_sent_once():
    broker, fake, signer, *_ = setup()
    order = signed(signer)
    broker.submit(order)
    with pytest.raises(DuplicateOrder):
        broker.submit(order)
    assert len(fake.posts()) == 1


def test_less_than_one_share_sends_nothing():
    broker, fake, signer, *_ = setup()
    r = broker.submit(signed(signer, notional="50"))
    assert (r.ack.status, r.ack.reason) == (AckStatus.REJECTED, "BELOW_ONE_SHARE") and fake.calls == []


def test_a_sell_never_exceeds_what_the_broker_holds():
    fake = FakeAlpaca(positions=[{"symbol": "AAA", "qty": "3.5", "side": "long", "avg_entry_price": "90"}])
    broker, fake, signer, *_ = setup(fake)
    broker.submit(signed(signer, side=Side.SELL, notional="2000", limit="99.00"))
    assert json.loads(fake.posts()[0][3])["qty"] == "3"
    broker, fake, signer, *_ = setup(FakeAlpaca(positions=[]))
    r = broker.submit(signed(signer, side=Side.SELL, notional="2000", limit="99.00"))
    assert r.ack.reason == "BELOW_ONE_SHARE" and fake.posts() == []


# ------------------------------------------------------------------ definite and ambiguous broker answers
@pytest.mark.parametrize("status", [403, 422])
def test_definite_rejections_close_the_record(tmp_path, status):
    idem, _ = IdempotencyStore.open(tmp_path / "idem.jsonl")
    broker, fake, signer, *_ = setup(FakeAlpaca(post_status=status), idem=idem)
    order = signed(signer)
    r = broker.submit(order)
    assert (r.ack.status, r.ack.reason) == (AckStatus.REJECTED, f"HTTP_{status}")
    assert idem.get(order.order_id).phase is Phase.COMPLETED


@pytest.mark.parametrize("fake", [
    FakeAlpaca(post_error=TimeoutError()), FakeAlpaca(post_error=OSError("reset")), FakeAlpaca(post_status=500),
    FakeAlpaca(post_status=429), FakeAlpaca(echo={"symbol": "BBB"}), FakeAlpaca(echo={"qty": "90"}),
    FakeAlpaca(echo={"client_order_id": "ta1-" + "0" * 40}), FakeAlpaca("overfill"), FakeAlpaca("no_price"),
], ids=["timeout", "reset", "5xx", "429", "echo-symbol", "echo-qty", "echo-cid", "overfill", "no-price"])
def test_any_ambiguous_answer_halts_and_leaves_the_order_for_recovery(tmp_path, fake):
    idem, _ = IdempotencyStore.open(tmp_path / "idem.jsonl")
    broker, fake, signer, clock, _ = setup(fake, idem=idem)
    order = signed(signer)
    with pytest.raises(BrokerStateUnknown):
        broker.submit(order)
    rec = idem.get(order.order_id)
    if fake.posts() and not fake.post_error and fake.post_status == 200 and not fake.echo:
        assert rec.phase is Phase.RESERVED
    resolved, pending = resolve_unresolved(idem, broker.lookup_order, clock.now() + timedelta(seconds=10))
    at_broker = any(o["client_order_id"] == client_order_id(order) for o in fake.orders.values())
    # at the broker under our client_order_id => RESOLVED; otherwise, once expired => ABANDONED. Never resent.
    assert resolved and resolved[0].phase is (Phase.RESOLVED if at_broker else Phase.ABANDONED)
    assert pending == [] and len(fake.posts()) <= 1


def test_lookup_finds_or_misses_by_client_order_id():
    broker, fake, signer, *_ = setup()
    order = signed(signer)
    broker.submit(order)
    assert broker.lookup_order(client_order_id(order)).status is AckStatus.ACCEPTED
    assert broker.lookup_order("ta1-" + "f" * 40) is None


# ------------------------------------------------------------------ positions and account
def test_positions_retry_once_then_halt():
    fake = FakeAlpaca(positions=[{"symbol": "AAA", "qty": "5", "side": "long"},
                                 {"symbol": "BBB", "qty": "-2", "side": "short"},
                                 {"symbol": "CCC", "qty": "0", "side": "long"}])
    broker, fake, *_ = setup(fake)
    fake.positions_error = 1
    assert broker.broker_positions() == {"AAA": Decimal(5), "BBB": Decimal(-2)}
    fake.positions_error = 2
    with pytest.raises(BrokerStateUnknown):
        broker.broker_positions()
    fake.positions = [{"symbol": "AAA", "qty": "5", "side": "long"}] * 2
    with pytest.raises(BrokerStateUnknown):
        broker.broker_positions()


def test_account_summary_refuses_a_blocked_account():
    broker, fake, *_ = setup()
    assert broker.account_summary()["cash"] == Decimal(100000)
    fake.account = dict(FakeAlpaca.account, trading_blocked=True)
    with pytest.raises(BrokerStateUnknown):
        broker.account_summary()


# ------------------------------------------------------------------ allow-list and secrets
@pytest.mark.parametrize("method, path", [
    ("DELETE", "/v2/orders"), ("DELETE", "/v2/positions"), ("DELETE", "/v2/positions/AAA"),
    ("PATCH", f"/v2/orders/{uuid.uuid4()}"), ("GET", "/v2/orders?status=all"), ("POST", "/v2/positions"),
    ("GET", "/v2/account/configurations"), ("PATCH", "/v2/account/configurations"), ("POST", "/v2/watchlists"),
    ("GET", "/v2/orders:by_client_order_id?client_order_id=x&nested=true"), ("GET", "https://api.alpaca.markets/v2/account"),
])
def test_the_client_refuses_everything_outside_the_allow_list(method, path):
    fake, clock = FakeAlpaca(), SimulatedClock(T0)
    client = AlpacaTradingClient(fake, Secrets(), "alpaca-paper", clock)
    with pytest.raises(TransportError):
        client.request(method, path, {} if method == "POST" else None)
    assert fake.calls == []


def test_only_post_carries_a_body_and_secrets_stay_out_of_errors():
    fake, clock, secrets = FakeAlpaca(post_error=OSError("boom"), positions=[]), SimulatedClock(T0), Secrets()
    client = AlpacaTradingClient(fake, secrets, "alpaca-paper", clock)
    with pytest.raises(TransportError):
        client.request("GET", "/v2/positions", {"x": "y"})
    with pytest.raises(TransportError) as info:
        client.request("POST", "/v2/orders", {"symbol": "AAA"})
    assert secrets.secret not in str(info.value) and secrets.key not in str(info.value)


def test_the_live_host_appears_nowhere_in_the_execution_package():
    pkg = Path(__file__).resolve().parents[2] / "src" / "trading_agent" / "execution"
    text = "\n".join(p.read_text(encoding="utf-8") for p in pkg.glob("*.py"))
    assert "api.alpaca.markets" not in text.replace(PAPER_HOST, "")
    assert PAPER_HOST == "paper-api.alpaca.markets"
