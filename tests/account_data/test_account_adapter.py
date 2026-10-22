"""Read-only REST account adapter: provenance, staleness, malformed/delayed payloads, rate limits."""
import inspect
import json
from datetime import timedelta

import pytest

from trading_agent.account_data.http import HttpResponse, HttpTransport, ReadOnlyHttpClient
from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.errors import ReadOnlyViolation
from trading_agent.testing.feeds import account_adapter, account_body, ok

C = DataIssueCode


def code(fetch):
    return fetch.issues[0].code if fetch.issues else None


def test_good_fetch_has_provenance_and_maps_to_snapshot():
    adapter, transport, _, clock = account_adapter()
    transport.push(ok(account_body(clock.now(), open_orders=[
        {"order_ref": "o-1", "symbol": "BBB US EQUITY", "side": "BUY", "notional": "1000", "limit_price": "49.5"}])))
    fetch = adapter.fetch()
    rec = fetch.record
    assert fetch.issues == [] and rec.source_id == "broker_a" and rec.account_alias == "main"
    assert rec.source_ts == clock.now() and rec.received_at == clock.now()
    assert set(rec.positions) == {"AAA"} and rec.open_orders[0].instrument == "BBB"
    snap = rec.to_account_snapshot()
    assert snap.quantity("AAA") == 10 and snap.open_orders[0].symbol == "BBB"
    assert transport.calls == ["/v1/account"]


@pytest.mark.parametrize("body,expected", [
    (b"not json", C.MALFORMED_MESSAGE),
    (b"[]", C.MALFORMED_MESSAGE),
    ("extra", C.MALFORMED_MESSAGE),
    ("naive_ts", C.MALFORMED_MESSAGE),
    ("unknown_symbol", C.UNKNOWN_INSTRUMENT),
    ("duplicate_position", C.MALFORMED_MESSAGE),
    ("bad_side", C.MALFORMED_MESSAGE),
    ("negative_avg_price", C.MALFORMED_MESSAGE),
])
def test_malformed_account_payload_rejected_whole(body, expected):
    adapter, transport, _, clock = account_adapter()
    now = clock.now()
    variants = {
        "extra": account_body(now, account_number="12345678"),
        "naive_ts": json.dumps({**json.loads(account_body(now)), "as_of": "2026-01-05T14:30:00"}).encode(),
        "unknown_symbol": account_body(now, positions=[{"symbol": "ZZZ", "quantity": "1", "avg_price": "1"}]),
        "duplicate_position": account_body(now, positions=[
            {"symbol": "AAA", "quantity": "1", "avg_price": "1"},
            {"symbol": "AAA US EQUITY", "quantity": "2", "avg_price": "1"}]),
        "bad_side": account_body(now, open_orders=[
            {"order_ref": "o", "symbol": "AAA", "side": "SHORT", "notional": "1", "limit_price": "1"}]),
        "negative_avg_price": account_body(now, positions=[{"symbol": "AAA", "quantity": "1", "avg_price": "-1"}]),
    }
    transport.push(ok(variants.get(body, body)))
    fetch = adapter.fetch()
    assert fetch.record is None and code(fetch) is expected


def test_delayed_account_is_stale():
    adapter, transport, _, clock = account_adapter()
    transport.push(ok(account_body(clock.now() - timedelta(seconds=30))))
    fetch = adapter.fetch()
    assert fetch.record is None and code(fetch) is C.ACCOUNT_STALE


def test_future_dated_account_rejected():
    adapter, transport, _, clock = account_adapter()
    transport.push(ok(account_body(clock.now() + timedelta(seconds=30))))
    assert code(adapter.fetch()) is C.FUTURE_TIMESTAMP


def test_older_version_rejected():
    adapter, transport, _, clock = account_adapter()
    transport.push(ok(account_body(clock.now(), version=5)), ok(account_body(clock.now(), version=4)))
    assert adapter.fetch().record is not None
    fetch = adapter.fetch()
    assert fetch.record is None and code(fetch) is C.OUT_OF_ORDER


def test_http_429_honours_retry_after_and_blocks_until_then():
    adapter, transport, _, clock = account_adapter()
    transport.push(HttpResponse(429, {"Retry-After": "10"}, b""), ok(account_body(clock.now() + timedelta(seconds=11))))
    assert code(adapter.fetch()) is C.RATE_LIMITED
    clock.advance(5)
    assert code(adapter.fetch()) is C.RATE_LIMITED and len(transport.calls) == 1   # no request sent
    clock.advance(6)
    assert adapter.fetch().record is not None


def test_client_side_rate_limit():
    adapter, transport, _, clock = account_adapter(rate=1, capacity=1)
    transport.push(ok(account_body(clock.now())), ok(account_body(clock.now())))
    assert adapter.fetch().record is not None
    fetch = adapter.fetch()
    assert code(fetch) is C.RATE_LIMITED and "local=True" in fetch.issues[0].detail
    assert len(transport.calls) == 1


@pytest.mark.parametrize("step,expected", [
    (HttpResponse(500, {}, b""), C.UPSTREAM_ERROR),
    (HttpResponse(503, {}, b""), C.UPSTREAM_ERROR),
    (HttpResponse(401, {}, b""), C.AUTH_REJECTED),
    (HttpResponse(403, {}, b""), C.AUTH_REJECTED),
    (TimeoutError(), C.UPSTREAM_ERROR),
    (ConnectionResetError(), C.UPSTREAM_ERROR),
])
def test_transport_failures_back_off(step, expected):
    adapter, transport, _, clock = account_adapter()
    transport.push(step)
    assert code(adapter.fetch()) is expected
    assert code(adapter.fetch()) is C.RATE_LIMITED and len(transport.calls) == 1  # in backoff


def test_backoff_grows_across_failures():
    adapter, transport, _, clock = account_adapter()
    waits = []
    for _ in range(4):
        transport.push(HttpResponse(503, {}, b""))
        adapter.fetch()
        wait = (adapter._next_attempt - clock.now()).total_seconds()
        waits.append(wait)
        clock.advance(wait)
    assert waits == sorted(waits) and waits[-1] > waits[0]


def test_client_is_get_only_and_allow_listed():
    _, transport, client, _ = account_adapter()
    with pytest.raises(ReadOnlyViolation):
        client.get("/v1/orders", None)
    public = {n for n in dir(ReadOnlyHttpClient) if not n.startswith("_")}
    assert public == {"get"}
    proto = {n for n, _ in inspect.getmembers(HttpTransport, inspect.isfunction) if not n.startswith("_")}
    assert proto == {"get"}
    assert transport.calls == []
