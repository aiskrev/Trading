"""ADR 0003 migration step 2: durable idempotency store, client_order_id, crash/recovery (acceptance test 4).

A crash between "record order_id" and "send" never produces a second broker order; recovery decides
sent / not-sent by broker lookup, and a lookup that fails means HALT (the caller's job, tested live).
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from test_order_signing import signed
from trading_agent.audit.chain import ChainCorrupted
from trading_agent.contracts.execution import AckStatus
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import AuditWriteFailure, DuplicateOrder
from trading_agent.core.mode import Mode
from trading_agent.core.signing import EphemeralSigner
from trading_agent.core.types import IdFactory
from trading_agent.execution import idempotency as idem_module
from trading_agent.execution.cost_model import CostModel
from trading_agent.execution.idempotency import (IdempotencyStore, Phase, client_order_id, resolve_unresolved)
from trading_agent.execution.simulated_broker import SimulatedBroker
from trading_agent.testing.harness import T0, market, verifier_for


class Crash(BaseException):
    """The process dies here. BaseException so no `except Exception` in the code under test can absorb it."""


@pytest.fixture()
def signer():
    return EphemeralSigner()


def broker_for(signer, store, clock=None, quote_for=None):
    clock = clock or SimulatedClock(T0)
    m = market()
    return SimulatedBroker(verifier_for(signer, clock), CostModel(), quote_for or (lambda s: m.quotes.get(s)), clock,
                           IdFactory("b"), idempotency=store), clock


# ------------------------------------------------------------------ client_order_id
def test_client_order_id_is_deterministic_and_pinned(signer):
    order = signed(signer)
    cid = client_order_id(order)
    assert cid == client_order_id(signed(EphemeralSigner("another-key")))      # not tied to the key or signature
    assert cid.startswith("ta1-") and len(cid) == 44 and cid[4:].isalnum()
    # Pinned (sha256 of the canonical JSON of order_id/environment/account, first 40 hex): changing the
    # derivation would stop recovery from finding orders already at a broker.
    assert cid == "ta1-dbfb2c803499f4b85353467c58d865ef6ae581ee"


def test_client_order_id_differs_by_order_environment_and_account(signer):
    base = client_order_id(signed(signer))
    others = {client_order_id(signed(signer, order_id="o2")),
              client_order_id(signed(signer, environment=Mode.PAPER)),
              client_order_id(signed(signer, broker_account_alias="other"))}
    assert base not in others and len(others) == 3


def test_client_order_id_ignores_everything_a_retry_may_change(signer):
    a = signed(signer)
    b = signed(signer, notional=Decimal("1"), issued_at=T0 + timedelta(seconds=1),
               expires_at=T0 + timedelta(seconds=6))
    assert client_order_id(a) == client_order_id(b)       # same order_id => same broker id => broker dedupes


# ------------------------------------------------------------------ store
def test_reserve_twice_is_a_duplicate_even_in_memory(signer):
    store = IdempotencyStore.in_memory()
    store.reserve(signed(signer), T0)
    with pytest.raises(DuplicateOrder):
        store.reserve(signed(signer), T0)
    assert not store.durable and len(store) == 1


def test_records_survive_a_restart_and_block_replay(tmp_path, signer):
    path = tmp_path / "idem.jsonl"
    store, _ = IdempotencyStore.open(path)
    broker, _ = broker_for(signer, store)
    order = signed(signer)
    first = broker.submit(order)
    assert first.fills
    assert store.get("o1").phase is Phase.COMPLETED and store.get("o1").outcome == "ACCEPTED"

    store2, opened = IdempotencyStore.open(path)                           # process restarted
    assert opened.entries == 2 and store2.get("o1").phase is Phase.COMPLETED and not store2.unresolved()
    fresh_broker, _ = broker_for(signer, store2)                           # knows nothing of the first one
    with pytest.raises(DuplicateOrder):
        fresh_broker.submit(order)
    assert fresh_broker.broker_positions() == {}                           # nothing was sent the second time


def test_reservation_is_fsynced(tmp_path, signer, monkeypatch):
    calls = []
    monkeypatch.setattr(idem_module.os, "fsync", lambda fd: calls.append(fd))
    store, _ = IdempotencyStore.open(tmp_path / "idem.jsonl")
    store.reserve(signed(signer), T0)
    assert len(calls) == 1


def test_unwritable_store_means_the_order_is_not_sent(tmp_path, signer):
    store, _ = IdempotencyStore.open(tmp_path / "idem.jsonl")
    sent, m = [], market()
    broker, _ = broker_for(signer, store, quote_for=lambda s: sent.append(s) or m.quotes.get(s))
    store._log.fail_writes = True
    with pytest.raises(AuditWriteFailure):
        broker.submit(signed(signer))
    assert sent == [] and broker.broker_positions() == {} and len(store) == 0   # memory never ran ahead of disk
    store._log.fail_writes = False
    assert broker.submit(signed(signer)).fills                                  # the same order is still sendable


def test_torn_tail_is_moved_aside_and_earlier_records_hold(tmp_path, signer):
    path = tmp_path / "idem.jsonl"
    store, _ = IdempotencyStore.open(path)
    store.reserve(signed(signer), T0)
    store.close("o1", Phase.COMPLETED, "ACCEPTED", T0)
    store.reserve(signed(signer, order_id="o2"), T0)
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])                               # crash while writing o2's reservation
    store2, opened = IdempotencyStore.open(path)
    assert opened.torn_line_moved_to and opened.torn_line_moved_to.exists()
    assert store2.get("o1").phase is Phase.COMPLETED and store2.get("o2") is None
    # a torn reservation was never fully recorded, hence never sent: it may be reserved (and sent) afresh
    store2.reserve(signed(signer, order_id="o2"), T0)


def test_tampered_store_refuses_to_open(tmp_path, signer):
    path = tmp_path / "idem.jsonl"
    store, _ = IdempotencyStore.open(path)
    store.reserve(signed(signer), T0)
    store.close("o1", Phase.COMPLETED, "ACCEPTED", T0)
    lines = path.read_bytes().split(b"\n")
    lines[0] = lines[0].replace(b'"o1"', b'"o9"', 1)
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(ChainCorrupted):
        IdempotencyStore.open(path)


def test_close_requires_a_reservation(tmp_path):
    store = IdempotencyStore.in_memory()
    with pytest.raises(KeyError):
        store.close("nope", Phase.COMPLETED, "ACCEPTED", T0)


# ------------------------------------------------------------------ acceptance test 4: crash windows
def test_crash_between_record_and_send_never_sends_twice(tmp_path, signer):
    path = tmp_path / "idem.jsonl"
    store, _ = IdempotencyStore.open(path)

    def die(_symbol):
        raise Crash()
    broker, clock = broker_for(signer, store, quote_for=die)
    order = signed(signer)
    with pytest.raises(Crash):
        broker.submit(order)                                   # reserved, then the process dies before sending
    assert broker.broker_positions() == {}

    store2, _ = IdempotencyStore.open(path)                    # restart
    assert [r.order_id for r in store2.unresolved()] == ["o1"]
    broker2, clock2 = broker_for(signer, store2)               # the broker never saw it
    with pytest.raises(DuplicateOrder):                        # a retry of the same order is refused, not sent
        broker2.submit(order)
    assert broker2.broker_positions() == {}

    # inside the signed lifetime the order could still be in flight: stays unresolved (=> caller HALTs)
    resolved, pending = resolve_unresolved(store2, broker2.lookup_order, T0 + timedelta(seconds=3))
    assert resolved == [] and [r.order_id for r in pending] == ["o1"]
    # after expiry + skew it can no longer be accepted anywhere: abandoned, never resent
    resolved, pending = resolve_unresolved(store2, broker2.lookup_order, T0 + timedelta(seconds=6.5))
    assert [(r.order_id, r.phase, r.outcome) for r in resolved] == [("o1", Phase.ABANDONED, "NOT_AT_BROKER")]
    assert pending == [] and broker2.broker_positions() == {}
    with pytest.raises(DuplicateOrder):
        broker2.submit(order)                                  # still refused: a new proposal needs a new order_id
    assert IdempotencyStore.open(path)[0].get("o1").phase is Phase.ABANDONED


def test_crash_after_send_before_close_is_resolved_by_broker_lookup(tmp_path, signer):
    path = tmp_path / "idem.jsonl"
    store, _ = IdempotencyStore.open(path)
    broker, _ = broker_for(signer, store)
    real_close = store.close

    def die(*a, **k):
        raise Crash()
    store.close = die
    order = signed(signer)
    with pytest.raises(Crash):
        broker.submit(order)                                   # filled at the broker, then the process dies
    store.close = real_close
    filled = broker.broker_positions()
    assert filled

    store2, _ = IdempotencyStore.open(path)
    assert [r.order_id for r in store2.unresolved()] == ["o1"]
    broker2, _ = broker_for(signer, store2)                    # the broker's own book survived (separate file)
    broker2.restore_positions(filled)
    broker2.restore_orders(broker.order_book())
    resolved, pending = resolve_unresolved(store2, broker2.lookup_order, T0 + timedelta(seconds=1))
    assert [(r.order_id, r.phase, r.outcome) for r in resolved] == [("o1", Phase.RESOLVED, "ACCEPTED")]
    assert pending == [] and broker2.broker_positions() == filled
    with pytest.raises(DuplicateOrder):
        broker2.submit(order)
    assert broker2.broker_positions() == filled                # no second fill


def test_a_failing_lookup_is_not_swallowed(tmp_path, signer):
    store = IdempotencyStore.in_memory()
    store.reserve(signed(signer), T0)

    def broken(_cid):
        raise ConnectionError("broker unreachable")
    with pytest.raises(ConnectionError):
        resolve_unresolved(store, broken, T0 + timedelta(minutes=1))
    assert [r.order_id for r in store.unresolved()] == ["o1"]    # still unresolved, not guessed


def test_restored_order_book_roundtrips(signer):
    store = IdempotencyStore.in_memory()
    broker, _ = broker_for(signer, store)
    broker.submit(signed(signer))
    book = json.loads(json.dumps({c: a.model_dump(mode="json") for c, a in broker.order_book().items()}))
    clone, _ = broker_for(signer, IdempotencyStore.in_memory())
    from trading_agent.contracts.execution import BrokerAck
    clone.restore_orders({c: BrokerAck.model_validate(a) for c, a in book.items()})
    assert clone.lookup_order(client_order_id(signed(signer))).status is AckStatus.ACCEPTED
    assert clone.lookup_order("ta1-unknown") is None
