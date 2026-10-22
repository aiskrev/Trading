"""Adapters feed the existing Normalizer -> MarketSnapshot -> TradingPipeline path unchanged."""
from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.core.trading_state import TradingState
from trading_agent.normalization.normalizer import Normalizer
from trading_agent.testing.feeds import feed, quote_msg
from trading_agent.testing.harness import PASS_JSON, Harness, trade_json


def snapshot_from(primary, secondary, clock, status=None):
    issues = list(primary.poll().issues) + list(secondary.poll().issues)
    result = Normalizer("vendor_a", "vendor_b").build(primary.fresh_ticks() + secondary.fresh_ticks(),
                                                      as_of=clock.now(), received_at=clock.now(),
                                                      venue_status=VenueStatus.OPEN, instrument_status=status)
    return result.snapshot, issues + result.issues


def test_missing_halt_status_denies():
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    now = h.clock.now()
    primary, _, _ = feed(sessions=[[quote_msg(1, now)]], clock=h.clock, source_id="vendor_a")
    secondary, _, _ = feed(sessions=[[]], clock=h.clock, source_id="vendor_b")
    snap, issues = snapshot_from(primary, secondary, h.clock)
    h.current = snap
    result = h.pipeline.tick(snap, h.features.update(snap), issues)
    assert result.order is None and "INSTRUMENT_STATUS_UNKNOWN" in result.decision.reason_codes


def test_two_live_style_feeds_into_pipeline():
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    now = h.clock.now()
    primary, _, _ = feed(sessions=[[quote_msg(1, now)]], clock=h.clock, source_id="vendor_a")
    secondary, _, _ = feed(sessions=[[quote_msg(1, now, symbol="AAA", bid="99.98", ask="100.02")]],
                           clock=h.clock, source_id="vendor_b")
    snap, issues = snapshot_from(primary, secondary, h.clock, status={"AAA": InstrumentStatus.TRADING})
    assert set(snap.quotes) == {"AAA"} and snap.source_ids == ["vendor_a", "vendor_b"]
    h.current = snap
    result = h.pipeline.tick(snap, h.features.update(snap), issues)
    assert result.order is not None


def test_dead_feed_means_no_quote_means_no_order():
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    primary, _, _ = feed(sessions=[[b"garbage"]], clock=h.clock, source_id="vendor_a")
    secondary, _, _ = feed(sessions=[[]], clock=h.clock, source_id="vendor_b")
    snap, issues = snapshot_from(primary, secondary, h.clock)
    assert snap.quotes == {}
    h.current = snap
    result = h.pipeline.tick(snap, h.features.update(snap), issues)
    assert result.order is None and "NO_QUOTE" in result.decision.reason_codes
    assert h.state.state is TradingState.NORMAL  # adapter issues alone do not halt; nothing is tradable
