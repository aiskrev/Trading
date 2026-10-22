"""AT-14 (fault injection => no new risk, state escalates, audited) and AT-18 (reconciliation)."""
from datetime import timedelta
from decimal import Decimal

from trading_agent.contracts.audit import AuditEventType
from trading_agent.core.errors import LLMUnavailable
from trading_agent.core.trading_state import TradingState
from trading_agent.normalization.validators import DataIssue, DataIssueCode
from trading_agent.testing.harness import PASS_JSON, Harness, market, trade_json


def harness(**kw):
    return Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON, **kw)


def events(h):
    return [r.event_type for r in h.ledger.records]


def test_source_divergence_goes_reduce_only_and_blocks_new_buys():
    h = harness()
    r = h.tick(issues=[DataIssue(code=DataIssueCode.SOURCE_DIVERGENCE, symbol="AAA")])
    assert h.state.state is TradingState.REDUCE_ONLY
    assert r.order is None and "REDUCE_ONLY_STATE" in r.decision.reason_codes
    assert AuditEventType.DATA_FAULT in events(h) and AuditEventType.STATE_CHANGE in events(h)


def test_reduce_only_still_allows_exits():
    h = Harness(planner_fn=lambda p: trade_json() if not p["account_snapshot"]["positions"]
                else trade_json(side="SELL", limit="99.99", notional="1000"), critic_fn=lambda p: PASS_JSON)
    assert h.tick().order is not None                                   # open a position
    h.state.trip(TradingState.REDUCE_ONLY, "test", h.clock.now())
    exit_ = h.tick(advance_seconds=1)
    assert exit_.order is not None and exit_.order.side.value == "SELL"


def test_clock_skew_halts_without_calling_planner():
    calls = []
    h = Harness(planner_fn=lambda p: calls.append(1) or trade_json(), critic_fn=lambda p: PASS_JSON)
    r = h.tick(market(as_of=h.clock.now() + timedelta(seconds=10)))
    assert h.state.state is TradingState.HALTED and r.skipped == "HALTED" and calls == []
    assert h.tick(advance_seconds=20).skipped == "HALTED"  # stays halted


def test_llm_timeout_means_no_trade():
    def timeout(p):
        raise LLMUnavailable("timeout")
    h = Harness(planner_fn=timeout, critic_fn=lambda p: PASS_JSON)
    r = h.tick()
    assert r.order is None and AuditEventType.SCHEMA_FAILURE in events(h)
    assert h.broker.broker_positions() == {}


def test_critic_timeout_means_deny():
    def timeout(p):
        raise LLMUnavailable("timeout")
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=timeout)
    r = h.tick()
    assert r.order is None and "CRITIC_DENY" in r.decision.reason_codes


def test_three_consecutive_broker_rejects_go_reduce_only():
    h = harness()
    h.broker.faults["reject_next"] = 3
    for i in range(3):
        r = h.tick(advance_seconds=31 if i else 0)  # stay under the per-instrument rate limit
        assert r.execution.ack.status.value == "REJECTED"
    assert h.state.state is TradingState.REDUCE_ONLY
    assert h.broker.broker_positions() == {}


def test_partial_fill_is_booked_and_reconciles():
    h = harness()
    h.broker.faults["partial_fill_ratio"] = Decimal("0.4")
    r = h.tick()
    exec_rec = [x for x in h.ledger.records if x.event_type is AuditEventType.EXECUTION][0]
    assert exec_rec.attribution.broker_result == "PARTIAL"
    assert h.portfolio.quantities() == h.broker.broker_positions()
    assert h.state.state is TradingState.NORMAL and r.order is not None


def test_reconciliation_mismatch_halts():
    """AT-18: broker books a fill it never reports => internal book diverges => HALTED."""
    h = harness()
    h.broker.faults["silent_fill_next"] = 1
    h.tick()
    assert h.state.state is TradingState.HALTED
    rec = [x for x in h.ledger.records if x.event_type is AuditEventType.RECONCILIATION]
    assert rec and rec[0].details["reason"] == "POSITION_MISMATCH"
    assert h.tick(advance_seconds=1).skipped == "HALTED"


def test_audit_write_failure_halts_and_prevents_submission():
    h = harness()
    h.ledger.fail_writes = True
    r = h.tick()
    assert r.order is None and r.skipped == "AUDIT_FAILURE"
    assert h.state.state is TradingState.HALTED
    assert h.broker.broker_positions() == {}


def test_unknown_broker_exception_halts():
    h = harness()

    def explode(order):
        raise ConnectionError("broker gone")
    h.broker.submit = explode
    h.tick()
    assert h.state.state is TradingState.HALTED


def test_crossed_quote_drops_symbol_and_is_audited():
    h = harness()
    snap = market(quotes={"BBB": market().quotes["BBB"]})  # AAA dropped by normalizer upstream
    r = h.tick(snap, issues=[DataIssue(code=DataIssueCode.CROSSED_QUOTE, symbol="AAA")])
    assert r.order is None and "NO_QUOTE" in r.decision.reason_codes
    assert h.state.state is TradingState.NORMAL
    assert AuditEventType.DATA_FAULT in events(h)
