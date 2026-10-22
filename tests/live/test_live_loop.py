"""Live run loop on mock feeds (BACKTEST): DecisionPackets, metrics, fail-closed ops, crash/restart recovery."""
import json

import pytest

from trading_agent.audit.chain import ChainedLog
from trading_agent.audit.ledger import AuditLedger
from trading_agent.contracts.decision_packet import DecisionPacket
from trading_agent.core.errors import AuditWriteFailure, LLMUnavailable, ModeNotPermitted
from trading_agent.core.mode import RuntimeConfig
from trading_agent.core.trading_state import TradingState
from trading_agent.orchestration.session_metrics import load_and_compute, render
from trading_agent.testing.harness import runtime_config
from trading_agent.testing.live import ManualClock, at, drive, make_runner

SESSION = "2026-01-13"


def fresh(tmp_path, start="18:50", **kw):
    clock = ManualClock(at(start))
    runner, replay = make_runner(tmp_path / "run", clock, **kw)
    return runner, replay, clock


def packets(run_dir):
    log, _ = ChainedLog.open(run_dir / "packets.jsonl", DecisionPacket)
    return log.items


# ---------------------------------------------------------------- packets & metrics
def test_every_cycle_persists_a_packet_including_no_trade(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    out = drive(runner, replay, clock, 20)            # 18:50 .. 19:09, AAA dips at 19:00
    stored = packets(tmp_path / "run")
    assert len(stored) == len(out) == 20
    assert stored[0].outcome == "NO_TRADE" and stored[0].proposal.reason_code in ("MARKET_DATA_UNKNOWN", "NO_TRIGGER")
    assert stored[0].planner_raw_output is None                         # no trigger => model not consulted
    consulted = [p for p in stored if p.planner_raw_output]
    assert consulted and all(p.at >= at("19:00") for p in consulted)    # only on the 19:00 dip trigger
    assert all(p.proposal.reason_code for p in stored if p.outcome == "NO_TRADE" and not p.planner_raw_output)
    outcomes = {p.outcome for p in stored}
    assert "APPROVED" in outcomes or "REDUCED" in outcomes
    assert all(p.attribution.risk_policy_version == "risk-policy.v2" for p in stored)
    with_quotes = [p for p in stored if p.market.quotes]
    assert len(with_quotes) >= len(stored) - 1                          # only the very first poll is empty
    assert all(p.signal_to_proposal_ms >= 500 for p in with_quotes)     # 0.5 s quote age + model time
    assert "signature" not in (tmp_path / "run" / "packets.jsonl").read_text(encoding="utf-8")


def test_session_metrics_cover_every_measure(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 30)
    m = load_and_compute(tmp_path / "run", SESSION)
    assert m.decisions == 30 and m.simulated_fills >= 1
    assert m.signal_to_proposal.p50_ms is not None and m.slippage_bps_mean is not None
    assert m.max_gross_exposure > 0 and m.pnl is not None and m.max_drawdown >= 0
    assert m.model_errors == 0 and m.schema_failures == 0 and m.restarts == 0
    text = render(m)
    for label in ("signal-to-proposal", "risk denials", "simulated fills", "slippage", "gross exposure", "P&L",
                  "max drawdown", "model errors", "schema failures", "data gaps", "restarts"):
        assert label in text


# ---------------------------------------------------------------- fail closed on ops failures
def test_feed_outage_goes_reduce_only(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 3)
    replay.paused = True
    drive(runner, replay, clock, 4)                     # > 120 s with no usable quotes
    assert runner.state.state is TradingState.REDUCE_ONLY
    replay.paused = False
    drive(runner, replay, clock, 3)
    assert runner.state.state is TradingState.REDUCE_ONLY  # never de-escalates by itself
    assert load_and_compute(tmp_path / "run", SESSION).empty_market_cycles >= 3


def test_consecutive_model_errors_go_reduce_only(tmp_path):
    def broken(payload):
        raise LLMUnavailable("timeout")
    runner, replay, clock = fresh(tmp_path, planner_fn=broken)
    drive(runner, replay, clock, 10)                                    # no trigger yet: model never called
    assert runner.state.state is TradingState.NORMAL
    drive(runner, replay, clock, 3)                                     # 19:00-19:02: trigger, model fails x3
    assert runner.state.state is TradingState.REDUCE_ONLY
    assert load_and_compute(tmp_path / "run", SESSION).model_errors == 3


def test_schema_failures_are_counted_and_never_trade(tmp_path):
    runner, replay, clock = fresh(tmp_path, planner_fn=lambda p: "Sure! {not json}")
    out = drive(runner, replay, clock, 20)
    assert all(p.fills == [] for p in out)
    consulted = sum(1 for p in out if p.planner_raw_output)
    assert consulted >= 1 and load_and_compute(tmp_path / "run", SESSION).schema_failures == consulted


def test_packet_write_failure_halts(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 2)
    runner.packets.fail_writes = True
    drive(runner, replay, clock, 1)
    assert runner.state.state is TradingState.HALTED and runner.stopped == "PACKET_WRITE_FAILURE"


def test_clock_going_backwards_halts(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 2)
    clock.set(at("18:40"))
    p = runner.step()
    assert runner.state.state is TradingState.HALTED and "CLOCK_WENT_BACKWARDS" in p.errors


def test_unexpected_exception_halts_and_is_recorded(tmp_path, monkeypatch):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 2)
    monkeypatch.setattr(runner.normalizer, "build", lambda *a, **k: 1 / 0)
    p = runner.step()
    assert runner.state.state is TradingState.HALTED and runner.stopped.startswith("OPERATIONAL_ERROR")
    assert packets(tmp_path / "run")[-1].errors == p.errors and "ZeroDivisionError" in p.errors[0]


def test_paper_mode_is_refused_until_the_owner_permits_it(tmp_path):
    cfg = RuntimeConfig.model_validate({**runtime_config().model_dump(), "mode": "PAPER"})
    with pytest.raises(ModeNotPermitted):
        fresh(tmp_path, config=cfg)


# ---------------------------------------------------------------- crash and restart
def _crash_after_entry(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 12)                                   # entry at 19:00 is filled
    held = runner.portfolio.quantities()
    assert held, "fixture should have produced a position"
    return held, clock


def test_restart_in_session_restores_book_and_fails_closed(tmp_path):
    held, clock = _crash_after_entry(tmp_path)                         # process dies here
    runner2, replay2 = make_runner(tmp_path / "run", clock, start=clock.now())
    assert runner2.recovery["kind"] == "restart_same_session" and runner2.recovery["reconciled"]
    assert runner2.state.state is TradingState.REDUCE_ONLY             # never resumes better than the floor
    assert runner2.portfolio.quantities() == held == runner2.broker.broker_positions()
    drive(runner2, replay2, clock, 10)
    run = tmp_path / "run"
    ledger, _ = AuditLedger.resume(run / "ledger.jsonl")
    assert AuditLedger.verify(ledger.records)                           # one unbroken chain across segments
    orders = [r.approved_order["order_id"] for r in ledger.records if r.approved_order]
    assert len(orders) == len(set(orders))                              # no duplicate order ids after restart
    stored = packets(run)
    assert {p.run_segment for p in stored} == {"paper.s0", "paper.s1"}
    m = load_and_compute(run, SESSION)
    assert m.restarts == 1 and m.recoveries[0]["state_after"] == "REDUCE_ONLY"


def test_torn_execution_record_is_detected_and_halts(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    path = tmp_path / "run" / "ledger.jsonl"
    data = path.read_bytes()
    lines = data.rstrip(b"\n").split(b"\n")
    exec_idx = max(i for i, l in enumerate(lines) if b'"event_type":"EXECUTION"' in l)
    kept = b"\n".join(lines[:exec_idx]) + b"\n" + lines[exec_idx][: len(lines[exec_idx]) // 2]
    path.write_bytes(kept)                                              # crash mid-write of the fill record
    runner2, _ = make_runner(tmp_path / "run", clock, start=clock.now())
    assert runner2.recovery["torn_ledger_line"] and (tmp_path / "run" / "ledger.jsonl.torn").exists()
    assert not runner2.recovery["reconciled"]                           # broker has the fill, our book doesn't
    assert runner2.state.state is TradingState.HALTED


def test_corrupted_ledger_refuses_to_start(tmp_path):
    _, clock = _crash_after_entry(tmp_path)
    path = tmp_path / "run" / "ledger.jsonl"
    lines = path.read_bytes().split(b"\n")
    lines[3] = lines[3].replace(b'"NORMAL"', b'"HALTED"', 1)
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(AuditWriteFailure, match="hash chain broken"):
        make_runner(tmp_path / "run", clock, start=clock.now())


def test_halt_survives_into_the_next_session_until_acknowledged(tmp_path):
    runner, replay, clock = fresh(tmp_path)
    drive(runner, replay, clock, 2)
    runner.packets.fail_writes = True
    drive(runner, replay, clock, 1)                                     # HALTED
    nxt, _ = make_runner(tmp_path / "run", clock, session_id="2026-01-14", start=clock.now())
    assert nxt.state.state is TradingState.HALTED
    ledger, _ = AuditLedger.resume(tmp_path / "run" / "ledger.jsonl")
    halt = [r for r in ledger.records if r.details.get("to") == "HALTED"][-1]
    (tmp_path / "run" / "halt_ack.json").write_text(json.dumps(
        {"halt_record_hash": halt.record_hash, "acknowledged_by": "owner", "note": "disk fixed"}), encoding="utf-8")
    acked, _ = make_runner(tmp_path / "run", clock, session_id="2026-01-14", start=clock.now())
    assert acked.state.state is TradingState.NORMAL and acked.recovery.get("halt_acknowledged")


def test_reduce_only_does_not_carry_into_the_next_session(tmp_path):
    def broken(payload):
        raise LLMUnavailable("timeout")
    runner, replay, clock = fresh(tmp_path, planner_fn=broken)
    drive(runner, replay, clock, 13)
    assert runner.state.state is TradingState.REDUCE_ONLY
    nxt, _ = make_runner(tmp_path / "run", clock, session_id="2026-01-14", start=clock.now())
    assert nxt.state.state is TradingState.NORMAL
