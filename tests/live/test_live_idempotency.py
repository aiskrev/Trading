"""Live loop: the idempotency store is written before every send and settles crashed orders at restart."""
from datetime import timedelta
from decimal import Decimal

from test_live_loop import _crash_after_entry, fresh, packets
from trading_agent.audit.ledger import AuditLedger
from trading_agent.contracts.audit import AuditEventType
from trading_agent.contracts.execution import ApprovedOrder
from trading_agent.core.mode import Mode
from trading_agent.core.types import Side
from trading_agent.core.trading_state import TradingState
from trading_agent.execution.idempotency import IdempotencyStore, Phase, client_order_id
from trading_agent.testing.live import drive, make_runner


def store_of(run_dir):
    return IdempotencyStore.open(run_dir / "idempotency.jsonl")[0]


def _order(order_id, clock, ttl_s=5):
    return ApprovedOrder(order_id=order_id, decision_id="d", proposal_id="p", symbol="AAA", side=Side.BUY,
                         limit_price=Decimal("100"), notional=Decimal("1000"), issued_at=clock.now(),
                         expires_at=clock.now() + timedelta(seconds=ttl_s), environment=Mode.BACKTEST,
                         broker_account_alias="backtest-sim", leader_epoch=0, policy_version="v",
                         policy_sha256="0" * 64, market_snapshot_id="m", account_snapshot_id="a",
                         key_id="ephemeral", signature="00")


def _ghost(run_dir, order_id, clock, ttl_s):
    """The reservation a process leaves behind when it dies after recording an order and before closing it."""
    store_of(run_dir).reserve(_order(order_id, clock, ttl_s), clock.now())


def test_every_order_is_recorded_and_closed_with_its_client_order_id(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 12)
    run = tmp_path / "run"
    ledger, _ = AuditLedger.resume(run / "ledger.jsonl")
    ordered = [r.approved_order["order_id"] for r in ledger.records if r.approved_order]
    store = store_of(run)
    assert ordered and len(store) == len(ordered)
    assert all(store.get(o).phase is Phase.COMPLETED and store.get(o).outcome for o in ordered)
    executions = [r for r in ledger.records if r.event_type is AuditEventType.EXECUTION]
    ids = {r.details["order_id"]: r.details["client_order_id"] for r in executions}
    assert ids and all(ids[o] == store.get(o).client_order_id for o in ids)
    assert all(c.startswith("ta1-") for c in ids.values())
    assert (run / "idempotency.jsonl").exists() and not store.unresolved()


def test_restart_with_a_clean_store_settles_nothing(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    runner2, _ = make_runner(tmp_path / "run", clock, start=clock.now())
    assert runner2.recovery["kind"] == "restart_same_session"
    assert "orders_settled" not in runner2.recovery and runner2.state.state is TradingState.REDUCE_ONLY


def test_crash_after_send_before_close_is_resolved_from_the_broker_book(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    run = tmp_path / "run"
    path = run / "idempotency.jsonl"
    lines = path.read_bytes().rstrip(b"\n").split(b"\n")
    assert len(lines) % 2 == 0 and b'"phase":"COMPLETED"' in lines[-1]
    path.write_bytes(b"\n".join(lines[:-1]) + b"\n")                  # the close of the last order never landed
    last = store_of(run).unresolved()
    assert len(last) == 1
    runner2, _ = make_runner(run, clock, start=clock.now())
    assert runner2.recovery["orders_settled"] == [f"{last[0].order_id}:RESOLVED:ACCEPTED"]
    assert runner2.recovery["orders_unresolved"] == [] and runner2.recovery["reconciled"]
    assert runner2.state.state is TradingState.REDUCE_ONLY           # same-session restart floor, not HALTED
    assert store_of(run).get(last[0].order_id).phase is Phase.RESOLVED
    ledger, _ = AuditLedger.resume(run / "ledger.jsonl")
    assert any(r.details.get("reason") == "ORDER_SETTLED_AT_RESTART" for r in ledger.records)


def test_crash_before_send_is_abandoned_once_expired_and_never_resent(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    run = tmp_path / "run"
    _ghost(run, "ghost-1", clock, ttl_s=5)
    clock.advance(30)                                                # restart well after the order expired
    runner2, replay2 = make_runner(run, clock, start=clock.now())
    assert runner2.recovery["orders_settled"] == ["ghost-1:ABANDONED:NOT_AT_BROKER"]
    assert runner2.recovery["orders_unresolved"] == [] and runner2.state.state is TradingState.REDUCE_ONLY
    assert runner2.broker.lookup_order(client_order_id(_order("ghost-1", clock))) is None   # never reached it
    drive(runner2, replay2, clock, 5)
    assert store_of(run).get("ghost-1").phase is Phase.ABANDONED


def test_order_possibly_still_in_flight_halts_the_restart(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    run = tmp_path / "run"
    _ghost(run, "ghost-2", clock, ttl_s=5)                           # restart inside the signed lifetime
    runner2, replay2 = make_runner(run, clock, start=clock.now())
    assert runner2.recovery["orders_unresolved"] == ["ghost-2"]
    assert runner2.state.state is TradingState.HALTED
    out = drive(runner2, replay2, clock, 3)
    assert all(p.outcome.startswith("SKIPPED") or p.outcome == "NO_TRADE" for p in out)
    assert not any(p.approved_order for p in packets(run)[-3:])      # nothing new was approved or sent


def test_broker_lookup_failure_halts(tmp_path, monkeypatch):
    _, clock = _crash_after_entry(tmp_path)
    run = tmp_path / "run"
    _ghost(run, "ghost-3", clock, ttl_s=5)
    clock.advance(30)
    from trading_agent.execution.simulated_broker import SimulatedBroker

    def unreachable(self, cid):
        raise ConnectionError("broker unreachable")
    monkeypatch.setattr(SimulatedBroker, "lookup_order", unreachable)
    runner2, _ = make_runner(run, clock, start=clock.now())
    assert runner2.recovery["order_lookup_error"] == "ConnectionError"
    assert runner2.recovery["orders_unresolved"] == ["ghost-3"] and runner2.state.state is TradingState.HALTED
    assert store_of(run).get("ghost-3").phase is Phase.RESERVED       # not guessed


def test_orders_on_record_without_a_ledger_halt(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    run = tmp_path / "run"
    _ghost(run, "ghost-4", clock, ttl_s=5)
    runner2, _ = make_runner(tmp_path / "run", clock, start=clock.now())
    assert runner2.recovery["kind"] == "fresh_start" and runner2.state.state is TradingState.HALTED


def test_torn_reservation_is_reported_and_harmless(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    run = tmp_path / "run"
    path = run / "idempotency.jsonl"
    with path.open("ab") as fh:
        fh.write(b'{"seq":99,"prev_hash":"abc')                      # crash while writing a reservation
    runner2, _ = make_runner(run, clock, start=clock.now())
    assert runner2.recovery["torn_idempotency_line"] and (run / "idempotency.jsonl.torn").exists()
    assert runner2.state.state is TradingState.REDUCE_ONLY
