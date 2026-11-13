"""Roadmap P3 (Alpaca): WebSocket client, Alpaca stream transport and decoder, against a scripted fake server.

No network and no real credential: the "KEY_ID:SECRET" value is generated at runtime.
"""
import json
import logging
import random
import secrets as _secrets
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import SecretStr

from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import AuthRejected, SecretUnavailable, TransportError
from trading_agent.market_data.alpaca_stream import AlpacaQuoteDecoder, AlpacaStreamTransport
from trading_agent.market_data.backoff import ExponentialBackoff
from trading_agent.market_data.instruments import InstrumentRegistry, InstrumentSpec
from trading_agent.market_data.quote_book import QuoteBook
from trading_agent.market_data.streaming import ConnectionState, StreamingQuoteAdapter
from trading_agent.market_data.websocket import WebSocketClient
from fake_websocket import FakeAlpacaServer, server_frame, wait_until
from trading_agent.testing.harness import T0

SOURCE = "alpaca"
SYMBOLS = ["AAPL", "MSFT"]


class Secrets:
    def __init__(self, value=None):
        self.key, self.secret = "PK" + _secrets.token_hex(8).upper(), _secrets.token_urlsafe(24)
        self.value = value if value is not None else f"{self.key}:{self.secret}"

    def get(self, name):
        if name != "alpaca-paper":
            raise SecretUnavailable(name)
        return SecretStr(self.value)


class Mono:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def registry():
    return InstrumentRegistry([InstrumentSpec(symbol=s, asset_class="EQUITY") for s in SYMBOLS], "test")


def build(server_kw=None, secrets=None, ping_every_s=1e9):
    secrets = secrets or Secrets()
    server = FakeAlpacaServer(secrets.key, secrets.secret, **(server_kw or {}))
    clock, mono = SimulatedClock(T0), Mono()
    transport = AlpacaStreamTransport("iex", socket_factory=server.factory, monotonic=mono, ping_every_s=ping_every_s,
                                      answer_timeout_s=3.0)
    adapter = StreamingQuoteAdapter(SOURCE, transport, AlpacaQuoteDecoder(SOURCE, registry()), SYMBOLS, clock,
                                    QuoteBook(SOURCE, timedelta(seconds=5), timedelta(seconds=2)),
                                    backoff=ExponentialBackoff(0.5, 2.0, 30.0, rng=random.Random(1)),
                                    secrets=secrets, credential_name="alpaca-paper",
                                    heartbeat_timeout=timedelta(seconds=60))
    return adapter, server, transport, clock, mono, secrets


def q(sym="AAPL", bp=189.98, ap=190.02, bs=3, as_=4, t=None):
    return {"T": "q", "S": sym, "bx": "V", "bp": bp, "bs": bs, "ax": "V", "ap": ap, "as": as_, "c": ["R"], "z": "C",
            "t": (t or T0).strftime("%Y-%m-%dT%H:%M:%S.") + "123456789Z"}


def poll_until(adapter, predicate, tries=200):
    batches = []
    for _ in range(tries):
        batches.append(adapter.poll())
        if predicate(batches):
            return batches
        import time
        time.sleep(0.01)
    return batches


def records(batches):
    return [r for b in batches for r in b.records]


def issues(batches):
    return [i.code for b in batches for i in b.issues]


# ------------------------------------------------------------------ happy path
def test_quotes_flow_from_the_alpaca_stream_into_the_quote_book():
    adapter, server, _, _, _, secrets = build()
    adapter.poll()
    assert adapter.state is ConnectionState.CONNECTED
    sent = [json.loads(m) for m in server.received]
    assert [m["action"] for m in sent] == ["auth", "subscribe"]
    assert sent[1] == {"action": "subscribe", "quotes": SYMBOLS, "trades": SYMBOLS}
    server.push_text(json.dumps([q("AAPL"), q("MSFT", bp=410.10, ap=410.30)]))
    batches = poll_until(adapter, lambda b: len(records(b)) >= 2)
    aapl, msft = records(batches)
    assert (aapl.instrument, aapl.bid, aapl.ask, aapl.bid_size, aapl.ask_size) == \
        ("AAPL", Decimal("189.98"), Decimal("190.02"), Decimal(300), Decimal(400))
    assert aapl.last == Decimal("190.00") and aapl.source_ts.microsecond == 123456   # mid until a trade is seen
    assert msft.bid == Decimal("410.10")
    server.push_text([{"T": "t", "S": "AAPL", "i": 1, "x": "V", "p": 190.01, "s": 10, "t": T0.isoformat()}, q("AAPL")])
    later = records(poll_until(adapter, lambda b: len(records(b)) >= 1))
    assert later[0].last == Decimal("190.01")
    assert {r.instrument for r in adapter.book.fresh(T0)} == {"AAPL", "MSFT"}


def test_the_credential_goes_only_into_the_auth_message_and_never_into_logs(caplog):
    caplog.set_level(logging.DEBUG)
    adapter, server, _, _, _, secrets = build()
    adapter.poll()
    server.push_text([q()])
    batches = poll_until(adapter, lambda b: len(records(b)) >= 1)
    auth, *rest = server.received
    assert secrets.secret in auth and all(secrets.secret not in m and secrets.key not in m for m in rest)
    text = caplog.text + repr(adapter) + repr(adapter.health()) + repr(batches)
    assert secrets.secret not in text and secrets.key not in text


def test_a_ping_is_sent_and_its_pong_counts_as_a_heartbeat():
    adapter, server, transport, clock, mono, _ = build(ping_every_s=15)
    adapter.poll()
    mono.t = 16
    got = [transport.recv()]                 # sends the ping (the pong may already arrive in this call)
    assert wait_until(lambda: server.pings == 1)
    assert wait_until(lambda: got.append(transport.recv()) or b'{"T":"_heartbeat"}' in got)


# ------------------------------------------------------------------ fail closed
@pytest.mark.parametrize("code, issue", [(402, DataIssueCode.AUTH_REJECTED), (409, DataIssueCode.AUTH_REJECTED),
                                         (406, DataIssueCode.RATE_LIMITED)])
def test_auth_errors_back_off_without_connecting(code, issue):
    adapter, server, *_ = build({"auth_error": code})
    batch = adapter.poll()
    assert adapter.state is ConnectionState.BACKOFF and issue in [i.code for i in batch.issues]
    if code == 406:
        assert batch.health.next_attempt_at >= T0 + timedelta(seconds=60)


def test_wrong_credentials_and_badly_formed_secrets_are_rejected():
    adapter, server, _, _, _, secrets = build()
    server.secret = _secrets.token_urlsafe(24)          # not what the client sends
    assert DataIssueCode.AUTH_REJECTED in [i.code for i in adapter.poll().issues]
    adapter, server, *_ = build(secrets=Secrets(value="no-colon-here"))
    assert DataIssueCode.AUTH_REJECTED in [i.code for i in adapter.poll().issues]
    assert server.received == []                       # nothing was sent


def test_a_missing_secret_is_auth_unavailable():
    adapter, *_ = build()
    adapter._credential_name = "other-name"
    assert DataIssueCode.AUTH_UNAVAILABLE in [i.code for i in adapter.poll().issues]


def test_an_unconfirmed_subscription_is_a_failed_connect():
    adapter, *_ = build({"confirm": lambda symbols: symbols[:1]})
    batch = adapter.poll()
    assert adapter.state is ConnectionState.BACKOFF and DataIssueCode.DISCONNECTED in [i.code for i in batch.issues]


def test_one_sided_quotes_update_nothing_and_are_not_malformed():
    adapter, server, *_ = build()
    adapter.poll()
    server.push_text([q(bp=0), q(ap=0), {"T": "b", "S": "AAPL"}, {"T": "s", "S": "AAPL"}])
    batches = poll_until(adapter, lambda b: adapter.health().messages >= 4, tries=100)
    assert records(batches) == [] and adapter.health().malformed == 0


@pytest.mark.parametrize("frame, code", [
    ('{"T":"q"}', DataIssueCode.MALFORMED_MESSAGE),                       # not an array
    ('[{"T":"q","S":"AAPL","S":"MSFT"}]', DataIssueCode.MALFORMED_MESSAGE),  # duplicate key
    ('[{"T":"q","S":"AAPL","bp":NaN}]', DataIssueCode.MALFORMED_MESSAGE),
    (json.dumps([q("TSLA")]), DataIssueCode.UNKNOWN_INSTRUMENT),
    (json.dumps([{"T": "zz"}]), DataIssueCode.MALFORMED_MESSAGE),
    (json.dumps([{k: v for k, v in q().items() if k != "t"}]), DataIssueCode.MALFORMED_MESSAGE),
], ids=["object", "dup", "nan", "unknown-symbol", "unknown-type", "no-timestamp"])
def test_bad_messages_are_dropped_and_counted(frame, code):
    adapter, server, *_ = build()
    adapter.poll()
    server.push_text(frame)
    batches = poll_until(adapter, lambda b: code in issues(b), tries=100)
    assert code in issues(batches) and records(batches) == []
    assert adapter.state is ConnectionState.CONNECTED


def test_a_stream_error_or_a_server_close_drops_the_feed_and_clears_the_book():
    adapter, server, *_ = build()
    adapter.poll()
    server.push_text([q()])
    poll_until(adapter, lambda b: len(records(b)) >= 1)
    server.push_text([{"T": "error", "code": 500, "msg": "internal error"}])
    batches = poll_until(adapter, lambda b: DataIssueCode.UPSTREAM_ERROR in issues(b), tries=100)
    assert DataIssueCode.UPSTREAM_ERROR in issues(batches) and adapter.book.fresh(T0) == []
    adapter2, server2, *_ = build()
    adapter2.poll()
    server2.close()
    batches = poll_until(adapter2, lambda b: DataIssueCode.DISCONNECTED in issues(b), tries=100)
    assert adapter2.state is ConnectionState.BACKOFF


def test_the_transport_only_talks_to_alpaca_hosts_and_known_feeds():
    for host in ("evil.example.com", "stream.data.alpaca.markets.evil.com", "api.alpaca.markets"):
        with pytest.raises(ValueError):
            AlpacaStreamTransport("iex", host=host)
    with pytest.raises(ValueError):
        AlpacaStreamTransport("crypto")
    transport = AlpacaStreamTransport("test", host="stream.data.sandbox.alpaca.markets")
    assert transport._ws._path == "/v2/test"
    with pytest.raises(AuthRejected):
        transport.connect(None)


# ------------------------------------------------------------------ WebSocket client protocol
def _ws(server, **kw):
    ws = WebSocketClient("stream.data.alpaca.markets", "/v2/iex", socket_factory=server.factory, timeout_s=2, **kw)
    ws.connect()
    assert json.loads(ws.recv_blocking(2)) == [{"T": "success", "msg": "connected"}]
    return ws


def test_websocket_handshake_must_be_accepted_properly():
    with pytest.raises(TransportError):
        WebSocketClient("h", "/", socket_factory=FakeAlpacaServer(bad_accept=True).factory, timeout_s=2).connect()


def test_websocket_fragments_are_reassembled_and_pings_answered():
    server = FakeAlpacaServer()
    ws = _ws(server)
    server.push_raw(server_frame(0x1, b'[{"T":', fin=False) + server_frame(0x9, b"hi") +
                    server_frame(0x0, b'"b"}]', fin=True))
    assert json.loads(ws.recv_blocking(2)) == [{"T": "b"}]
    assert wait_until(lambda: True)


@pytest.mark.parametrize("raw", [
    server_frame(0x1, b"[]", masked=True),          # server frames must not be masked
    server_frame(0x2, b"\x00\x01"),                 # binary
    server_frame(0x1, b"[]", rsv=0x40),             # extension bits
    server_frame(0x9, b"x" * 200),                  # control frame too long
    server_frame(0x0, b"[]"),                       # continuation without a start
    server_frame(0x8, b"\x03\xe8"),                 # close
    server_frame(0x1, b"\xff\xfe"),                 # not utf-8
], ids=["masked", "binary", "rsv", "long-control", "orphan-continuation", "close", "not-utf8"])
def test_websocket_protocol_violations_end_the_connection(raw):
    server = FakeAlpacaServer()
    ws = _ws(server)
    server.push_raw(raw)
    with pytest.raises(TransportError):
        for _ in range(100):
            ws.poll(0.05)
    assert not ws.connected


def test_websocket_refuses_an_oversized_frame_header():
    server = FakeAlpacaServer()
    ws = _ws(server)
    server.push_raw(bytes([0x81, 127]) + (10 ** 9).to_bytes(8, "big"))
    with pytest.raises(TransportError):
        for _ in range(50):
            ws.poll(0.05)
