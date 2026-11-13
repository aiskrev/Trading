"""Alpaca clock and asset status (read-only), against recorded-shape responses. No network, no real keys."""
import json
import secrets as _secrets
from datetime import timedelta

import pytest
from pydantic import SecretStr

from trading_agent.account_data.alpaca_status import (ALLOWED_HOSTS, AlpacaMarketStatus, AlpacaReadOnlyClient,
                                                      PAPER_TRADING_HOST)
from trading_agent.account_data.http import HttpResponse
from trading_agent.account_data.https_transport import HttpsGetTransport
from trading_agent.contracts.market import InstrumentStatus, VenueStatus
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import SecretUnavailable, TransportError
from trading_agent.testing.harness import T0


class Secrets:
    def __init__(self):
        self.key, self.secret = "PK" + _secrets.token_hex(8).upper(), _secrets.token_urlsafe(24)
        self.available = True

    def get(self, name):
        if not self.available or name != "alpaca-paper":
            raise SecretUnavailable(name)
        return SecretStr(f"{self.key}:{self.secret}")


class Transport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path, headers, timeout_s):
        self.calls.append((path, dict(headers)))
        answer = self.routes.get(path, (404, {}))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return HttpResponse(status, {}, json.dumps(body).encode() if not isinstance(body, bytes) else body)


def clock_body(is_open=True, ts=T0, close_in_h=3):
    return {"timestamp": ts.isoformat(), "is_open": is_open, "next_open": (ts + timedelta(hours=20)).isoformat(),
            "next_close": (ts + timedelta(hours=close_in_h)).isoformat()}


def asset(symbol="AAPL", status="active", tradable=True):
    return {"id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "class": "us_equity", "exchange": "NASDAQ",
            "symbol": symbol, "name": "Apple Inc.", "status": status, "tradable": tradable, "marginable": True,
            "shortable": True, "easy_to_borrow": True, "fractionable": True}


def make(routes, clock=None, secrets=None):
    clock = clock or SimulatedClock(T0)
    secrets = secrets or Secrets()
    transport = Transport(routes)
    client = AlpacaReadOnlyClient(transport, secrets, "alpaca-paper", clock)
    return AlpacaMarketStatus(client, clock), transport, clock, secrets


def test_open_closed_and_cached_clock():
    status, transport, clock, secrets = make({"/v2/clock": (200, clock_body())})
    assert status.venue_status(T0) is VenueStatus.OPEN
    assert status.venue_status(T0 + timedelta(seconds=5)) is VenueStatus.OPEN
    assert len(transport.calls) == 1                                   # cached for 30 s
    path, headers = transport.calls[0]
    assert headers["APCA-API-KEY-ID"] == secrets.key and headers["APCA-API-SECRET-KEY"] == secrets.secret
    assert status.venue_status(T0 + timedelta(hours=4)) is VenueStatus.UNKNOWN   # past next_close: ask again
    status, *_ = make({"/v2/clock": (200, clock_body(is_open=False))})
    assert status.venue_status(T0) is VenueStatus.CLOSED


@pytest.mark.parametrize("answer", [
    (500, {}), (401, {}), (429, {}), (200, b"not json"), (200, {"is_open": True}),
    (200, clock_body(ts=T0 + timedelta(seconds=10))),                # Alpaca's clock and ours disagree
    OSError("connection reset"),
], ids=["5xx", "401", "429", "bad-json", "missing-fields", "clock-skew", "network"])
def test_any_clock_problem_is_unknown(answer):
    status, *_ = make({"/v2/clock": answer})
    assert status.venue_status(T0) is VenueStatus.UNKNOWN
    assert status.last_problem


def test_a_missing_secret_is_unknown_and_nothing_is_sent():
    secrets = Secrets()
    secrets.available = False
    status, transport, *_ = make({"/v2/clock": (200, clock_body())}, secrets=secrets)
    assert status.venue_status(T0) is VenueStatus.UNKNOWN and transport.calls == []


@pytest.mark.parametrize("body, expected", [
    (asset(), InstrumentStatus.TRADING),
    (asset(status="inactive"), InstrumentStatus.HALTED),
    (asset(tradable=False), InstrumentStatus.HALTED),
    (asset(symbol="MSFT"), InstrumentStatus.UNKNOWN),                   # an answer about another symbol
])
def test_asset_status_mapping(body, expected):
    status, *_ = make({"/v2/assets/AAPL": (200, body)})
    assert status.instrument_status("AAPL") is expected


def test_asset_status_is_cached_but_failures_are_not():
    routes = {"/v2/assets/AAPL": (503, {})}
    status, transport, clock, _ = make(routes)
    assert status.instrument_status("AAPL") is InstrumentStatus.UNKNOWN
    routes["/v2/assets/AAPL"] = (200, asset())
    assert status.instrument_status("AAPL") is InstrumentStatus.TRADING
    assert status.instrument_status("AAPL") is InstrumentStatus.TRADING
    assert len(transport.calls) == 2


def test_only_allow_listed_paths_and_symbols_are_requested():
    status, transport, clock, secrets = make({})
    for bad in ("../v2/account", "AAPL/../../v2/orders", "aapl", "", "A" * 20):
        assert status.instrument_status(bad) is InstrumentStatus.UNKNOWN
    assert transport.calls == []
    client = AlpacaReadOnlyClient(transport, secrets, "alpaca-paper", clock)
    for path in ("/v2/orders", "/v2/account", "/v2/positions", "/v2/clock?x=1"):
        with pytest.raises(TransportError):
            client.get_json(path)
    assert transport.calls == []


def test_the_https_transport_only_reaches_the_paper_host():
    assert ALLOWED_HOSTS == {PAPER_TRADING_HOST}
    for host in ("api.alpaca.markets", "evil.example.com"):
        with pytest.raises(ValueError):
            HttpsGetTransport(host, ALLOWED_HOSTS)
    t = HttpsGetTransport(PAPER_TRADING_HOST, ALLOWED_HOSTS)
    assert not hasattr(t, "post") and not hasattr(t, "delete")
    with pytest.raises(ValueError):
        t.get("/v2/clock\r\nX: y", {}, 1)
