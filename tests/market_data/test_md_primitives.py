"""Backoff with jitter, token bucket, sequence tracking, quote-book acceptance rules."""
import random
from datetime import timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.clock import SimulatedClock
from trading_agent.market_data.backoff import ExponentialBackoff
from trading_agent.market_data.quote_book import QuoteBook
from trading_agent.market_data.rate_limit import TokenBucket
from trading_agent.market_data.records import QuoteRecord
from trading_agent.market_data.sequence import SequenceTracker, SeqStatus
from trading_agent.testing.harness import T0


# ---------------------------------------------------------------- backoff
@settings(max_examples=200, deadline=None)
@given(seed=st.integers(0, 2**32), n=st.integers(0, 200))
def test_backoff_delay_is_bounded_and_jittered(seed, n):
    b = ExponentialBackoff(0.5, 2.0, 30.0, rng=random.Random(seed))
    for _ in range(n):
        b.next_delay()
    cap = b.cap(b.attempts)
    d = b.next_delay()
    assert cap / 2 <= d <= cap <= 30.0


def test_backoff_grows_then_caps_and_resets():
    b = ExponentialBackoff(0.5, 2.0, 8.0, rng=random.Random(1))
    caps = [b.cap(i) for i in range(6)]
    assert caps == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    for _ in range(5):
        b.next_delay()
    b.reset()
    assert b.attempts == 0 and b.next_delay() <= 0.5


def test_backoff_jitter_differs_between_clients():
    a = [ExponentialBackoff(rng=random.Random(1)).next_delay() for _ in range(1)]
    b = [ExponentialBackoff(rng=random.Random(2)).next_delay() for _ in range(1)]
    assert a != b


@pytest.mark.parametrize("args", [(0, 2, 30), (1, 0.5, 30), (5, 2, 1)])
def test_backoff_rejects_bad_params(args):
    with pytest.raises(ValueError):
        ExponentialBackoff(*args)


# ----------------------------------------------------------- token bucket
def test_token_bucket_limits_and_refills():
    clock = SimulatedClock(T0)
    bucket = TokenBucket(rate_per_s=2, capacity=2, clock=clock)
    assert bucket.try_acquire() == 0 and bucket.try_acquire() == 0
    wait = bucket.try_acquire()
    assert wait == pytest.approx(0.5)
    clock.advance(0.5)
    assert bucket.try_acquire() == 0


# --------------------------------------------------------------- sequence
def test_sequence_tracker():
    t = SequenceTracker()
    assert t.observe("*", 10).status is SeqStatus.FIRST
    assert t.observe("*", 11).status is SeqStatus.OK
    assert t.observe("*", 11).status is SeqStatus.DUPLICATE
    assert t.observe("*", 9).status is SeqStatus.OUT_OF_ORDER
    gap = t.observe("*", 15)
    assert gap.status is SeqStatus.GAP and gap.expected == 12
    assert t.observe("*", 16).status is SeqStatus.OK
    assert t.observe("AAA", 1).status is SeqStatus.FIRST  # independent keys


# -------------------------------------------------------------- quote book
def rec(ts, received=None, symbol="AAA", snapshot=False):
    return QuoteRecord(source_id="v", instrument=symbol, vendor_symbol=symbol, source_ts=ts,
                       received_at=received or ts, bid=1, ask=2, bid_size=1, ask_size=1, last=1.5,
                       is_snapshot=snapshot)


def book():
    return QuoteBook("v", max_age=timedelta(seconds=5), max_latency=timedelta(seconds=1))


def test_book_rejects_late_future_and_out_of_order():
    b = book()
    assert b.apply(rec(T0, T0 + timedelta(seconds=3))).code is DataIssueCode.LATE_MESSAGE
    assert b.apply(rec(T0 + timedelta(seconds=10), T0)).code is DataIssueCode.FUTURE_TIMESTAMP
    assert b.apply(rec(T0 + timedelta(seconds=2))) is None
    assert b.apply(rec(T0 + timedelta(seconds=1), T0 + timedelta(seconds=2))).code is DataIssueCode.OUT_OF_ORDER


def test_book_staleness():
    b = book()
    b.apply(rec(T0))
    assert [r.instrument for r in b.fresh(T0 + timedelta(seconds=5))] == ["AAA"]
    assert b.fresh(T0 + timedelta(seconds=6)) == []
    assert b.stale_issues(T0 + timedelta(seconds=6))[0].code is DataIssueCode.STALE_INSTRUMENT


def test_quarantine_until_snapshot():
    b = book()
    b.apply(rec(T0))
    b.quarantine(None)
    assert b.fresh(T0) == []
    assert b.apply(rec(T0 + timedelta(seconds=1))).code is DataIssueCode.SEQUENCE_GAP
    assert b.apply(rec(T0 + timedelta(seconds=1), symbol="BBB", snapshot=True)) is None
    assert [r.instrument for r in b.fresh(T0 + timedelta(seconds=1))] == ["BBB"]  # AAA still quarantined
    assert b.apply(rec(T0 + timedelta(seconds=2), snapshot=True)) is None
    assert {r.instrument for r in b.fresh(T0 + timedelta(seconds=2))} == {"AAA", "BBB"}
