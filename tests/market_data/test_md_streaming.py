"""Streaming adapter: delayed messages, gaps, reconnect with backoff, rate limits, staleness."""
from datetime import timedelta

from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import AuthRejected, RateLimited, TransportError
from trading_agent.market_data.streaming import ConnectionState
from trading_agent.testing.feeds import EphemeralTestSecrets, feed, heartbeat, quote_msg
from trading_agent.testing.harness import T0

C = DataIssueCode


def codes(batch):
    return [i.code for i in batch.issues]


def test_records_carry_source_and_timestamps():
    msgs = [quote_msg(1, T0), quote_msg(2, T0, symbol="BBB.US", bid="49.99", ask="50.01")]
    adapter, transport, clock = feed(sessions=[msgs], clock=SimulatedClock(T0))
    batch = adapter.poll()
    assert [r.instrument for r in batch.records] == ["AAA", "BBB"]
    for r in batch.records:
        assert r.source_id == "vendor_a" and r.source_ts == clock.now() and r.received_at == clock.now()
    assert transport.subscriptions == [["AAA.US", "BBB.US"]]
    assert {t.symbol for t in adapter.fresh_ticks()} == {"AAA", "BBB"}


def test_delayed_message_is_rejected_not_used():
    adapter, transport, clock = feed(sessions=[[]], max_latency_s=1.5)
    adapter.poll()
    clock.advance(10)
    transport.push(quote_msg(1, clock.now() - timedelta(seconds=3)))   # 3s late
    batch = adapter.poll()
    assert batch.records == [] and C.LATE_MESSAGE in codes(batch)
    assert adapter.fresh_ticks() == [] and batch.health.late == 1


def test_future_dated_message_rejected():
    adapter, transport, clock = feed(sessions=[[]])
    adapter.poll()
    transport.push(quote_msg(1, clock.now() + timedelta(seconds=30)))
    assert C.FUTURE_TIMESTAMP in codes(adapter.poll())


def test_quotes_go_stale():
    adapter, transport, clock = feed(sessions=[[]], max_age_s=5, heartbeat_s=60)
    adapter.poll()
    transport.push(quote_msg(1, clock.now()))
    adapter.poll()
    clock.advance(6)
    batch = adapter.poll()
    assert adapter.fresh_ticks() == [] and C.STALE_INSTRUMENT in codes(batch)


def test_malformed_messages_are_dropped_and_counted():
    adapter, transport, clock = feed(sessions=[[b"garbage", b"{}", quote_msg(1, T0)]])
    batch = adapter.poll()
    assert len(batch.records) == 1  # a good message after bad ones is still used
    assert codes(batch).count(C.MALFORMED_MESSAGE) == 2
    assert batch.health.malformed == 2 and adapter.state is ConnectionState.CONNECTED


def test_too_many_malformed_in_a_row_forces_reconnect():
    adapter, transport, clock = feed(sessions=[[b"x"] * 25])
    batch = adapter.poll()
    assert adapter.state is ConnectionState.BACKOFF and batch.health.malformed == 20


def test_feed_sequence_gap_quarantines_until_snapshot():
    adapter, transport, clock = feed(sessions=[[]])
    adapter.poll()
    now = clock.now()
    transport.push(quote_msg(1, now), quote_msg(2, now, symbol="BBB.US", bid="49.99", ask="50.01"))
    adapter.poll()
    clock.advance(1)
    transport.push(quote_msg(5, clock.now()))                       # 3 and 4 missing
    batch = adapter.poll()
    assert C.SEQUENCE_GAP in codes(batch) and transport.resync_requests == 1
    assert adapter.fresh_ticks() == []                              # whole feed quarantined
    transport.push(quote_msg(6, clock.now(), snapshot=True))
    adapter.poll()
    assert [t.symbol for t in adapter.fresh_ticks()] == ["AAA"]     # BBB still awaits its snapshot
    assert adapter.health().gaps == 1


def test_instrument_scope_gap_only_affects_that_instrument():
    adapter, transport, clock = feed(sessions=[[]], scope="instrument")
    adapter.poll()
    now = clock.now()
    transport.push(quote_msg(1, now), quote_msg(1, now, symbol="BBB.US", bid="49.99", ask="50.01"),
                   quote_msg(4, now))                               # AAA jumps 1 -> 4
    batch = adapter.poll()
    gap = [i for i in batch.issues if i.code is C.SEQUENCE_GAP][0]
    assert gap.symbol == "AAA"
    assert [t.symbol for t in adapter.fresh_ticks()] == ["BBB"]


def test_duplicate_and_reordered_messages_dropped():
    adapter, transport, clock = feed(sessions=[[]])
    adapter.poll()
    now = clock.now()
    transport.push(quote_msg(1, now), quote_msg(2, now), quote_msg(2, now), quote_msg(1, now))
    batch = adapter.poll()
    assert len(batch.records) == 2
    assert C.DUPLICATE_MESSAGE in codes(batch) and C.OUT_OF_ORDER in codes(batch)


def test_disconnect_clears_book_and_reconnects_with_growing_backoff():
    adapter, transport, clock = feed(
        sessions=[[], [], []],
        connect_errors=[None, TransportError("refused"), TransportError("refused"), None])
    adapter.poll()
    transport.push(quote_msg(1, clock.now()))
    adapter.poll()
    assert adapter.fresh_ticks()
    transport.push(TransportError("reset by peer"))
    batch = adapter.poll()
    assert C.DISCONNECTED in codes(batch) and adapter.fresh_ticks() == []      # held quotes invalidated
    delays = []
    for _ in range(2):                                                          # two failed reconnects
        wait = (adapter.health().next_attempt_at - clock.now()).total_seconds()
        delays.append(wait)
        assert adapter.poll().health.state is ConnectionState.BACKOFF          # not due yet: no attempt
        clock.advance(wait)
        adapter.poll()
    assert 0.25 <= delays[0] <= 0.5 <= delays[1] <= 1.0   # equal-jitter windows for attempts 0 and 1
    wait = (adapter.health().next_attempt_at - clock.now()).total_seconds()
    clock.advance(wait)
    assert adapter.poll().health.state is ConnectionState.CONNECTED
    assert adapter.health().reconnects == 1 and transport.connect_calls == 4


def test_no_reconnect_attempt_before_backoff_expires():
    adapter, transport, clock = feed(sessions=[[TransportError("x")], []])
    adapter.poll()
    calls = transport.connect_calls
    for _ in range(10):
        adapter.poll()
    assert transport.connect_calls == calls


def test_heartbeat_timeout_marks_feed_stale_and_reconnects():
    adapter, transport, clock = feed(sessions=[[], []], heartbeat_s=5)
    adapter.poll()
    clock.advance(3)
    transport.push(heartbeat(1, clock.now()))
    assert adapter.poll().health.state is ConnectionState.CONNECTED
    clock.advance(6)
    batch = adapter.poll()
    assert C.STALE_FEED in codes(batch) and adapter.state is ConnectionState.BACKOFF


def test_rate_limit_honours_retry_after():
    adapter, transport, clock = feed(sessions=[[RateLimited(12)], []])
    batch = adapter.poll()
    assert C.RATE_LIMITED in codes(batch) and batch.health.rate_limited == 1
    assert (batch.health.next_attempt_at - clock.now()).total_seconds() >= 12
    clock.advance(11)
    adapter.poll()
    assert transport.connect_calls == 1
    clock.advance(2)
    assert adapter.poll().health.state is ConnectionState.CONNECTED


def test_rate_limit_error_message_from_vendor():
    adapter, transport, clock = feed(sessions=[[b'{"type":"error","code":"RATE_LIMIT","retry_after_s":4}']])
    batch = adapter.poll()
    assert C.RATE_LIMITED in codes(batch) and adapter.state is ConnectionState.BACKOFF


def test_vendor_error_reconnects():
    adapter, transport, clock = feed(sessions=[[b'{"type":"error","code":"MAINTENANCE"}']])
    assert C.UPSTREAM_ERROR in codes(adapter.poll())


def test_auth_rejected_and_unavailable_back_off():
    secrets = EphemeralTestSecrets(["MD_TOKEN"])
    adapter, transport, clock = feed(connect_errors=[AuthRejected("401")], secrets=secrets, credential_name="MD_TOKEN")
    assert C.AUTH_REJECTED in codes(adapter.poll()) and adapter.state is ConnectionState.BACKOFF
    adapter2, transport2, _ = feed(secrets=secrets, credential_name="MISSING")
    assert C.AUTH_UNAVAILABLE in codes(adapter2.poll()) and transport2.connect_calls == 0


def test_backoff_resets_after_healthy_message():
    adapter, transport, clock = feed(sessions=[[TransportError("x")], [], []])
    adapter.poll()
    first_wait = (adapter.health().next_attempt_at - clock.now()).total_seconds()
    clock.advance(first_wait)
    adapter.poll()
    transport.push(quote_msg(1, clock.now()))
    adapter.poll()                                   # healthy message resets the backoff
    transport.push(TransportError("y"))
    adapter.poll()
    wait = (adapter.health().next_attempt_at - clock.now()).total_seconds()
    assert wait <= 0.5
