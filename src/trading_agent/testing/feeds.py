"""Builders for feed/account test messages and a runtime-generated test secret."""
import json
import random
import secrets as _stdlib_secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from pydantic import SecretStr

from trading_agent.account_data.http import HttpResponse, ReadOnlyHttpClient
from trading_agent.account_data.mock_http import ScriptedHttpTransport
from trading_agent.account_data.rest_adapter import ACCOUNT_PATH, RestAccountAdapter
from trading_agent.core.clock import SimulatedClock
from trading_agent.core.errors import SecretUnavailable
from trading_agent.market_data.backoff import ExponentialBackoff
from trading_agent.market_data.decoders import GenericJsonQuoteDecoder
from trading_agent.market_data.instruments import InstrumentRegistry
from trading_agent.market_data.mock_transport import ScriptedStreamTransport
from trading_agent.market_data.quote_book import QuoteBook
from trading_agent.market_data.rate_limit import TokenBucket
from trading_agent.market_data.streaming import StreamingQuoteAdapter
from trading_agent.testing.harness import ROOT, T0

REGISTRY_PATH = ROOT / "config" / "instruments.v1.yaml"


def registry() -> InstrumentRegistry:
    return InstrumentRegistry.load(REGISTRY_PATH)


class EphemeralTestSecrets:
    """Test-only provider. Values are generated at runtime, so no secret exists in source."""

    def __init__(self, names: List[str]) -> None:
        self._values: Dict[str, SecretStr] = {n: SecretStr(_stdlib_secrets.token_urlsafe(24)) for n in names}

    def get(self, name: str) -> SecretStr:
        if name not in self._values:
            raise SecretUnavailable(name)
        return self._values[name]

    def raw(self, name: str) -> str:
        return self._values[name].get_secret_value()


def quote_msg(seq: Optional[int], ts: datetime, symbol: str = "AAA.US", bid: str = "99.99", ask: str = "100.01",
              snapshot: bool = False, **override) -> bytes:
    body = {"type": "quote", "seq": seq, "symbol": symbol, "ts": ts.isoformat(), "bid": bid, "ask": ask,
            "bid_size": "500", "ask_size": "500", "last": "100.00", "snapshot": snapshot}
    if seq is None:
        del body["seq"]
    body.update(override)
    return json.dumps(body).encode("utf-8")


def heartbeat(seq: Optional[int], ts: datetime) -> bytes:
    body = {"type": "heartbeat", "ts": ts.isoformat()}
    if seq is not None:
        body["seq"] = seq
    return json.dumps(body).encode("utf-8")


def feed(sessions=(), connect_errors=(), clock: Optional[SimulatedClock] = None, source_id: str = "vendor_a",
         secrets=None, credential_name: Optional[str] = None, scope: str = "feed",
         max_latency_s: float = 1.5, max_age_s: float = 5.0, heartbeat_s: float = 5.0, seed: int = 7):
    clock = clock or SimulatedClock(T0)
    transport = ScriptedStreamTransport(sessions, connect_errors)
    reg = registry()
    adapter = StreamingQuoteAdapter(
        source_id, transport, GenericJsonQuoteDecoder(source_id, reg),
        [reg.vendor_symbol(source_id, s) for s in sorted(reg.symbols)], clock,
        QuoteBook(source_id, timedelta(seconds=max_age_s), timedelta(seconds=max_latency_s)),
        backoff=ExponentialBackoff(0.5, 2.0, 30.0, rng=random.Random(seed)),
        secrets=secrets, credential_name=credential_name,
        heartbeat_timeout=timedelta(seconds=heartbeat_s), sequence_scope=scope)
    return adapter, transport, clock


def account_body(as_of: datetime, version: Optional[int] = 1, positions=None, open_orders=None, **override) -> bytes:
    body = {"as_of": as_of.isoformat(), "version": version, "cash": "100000.00", "realized_pnl_today": "0",
            "orders_today": 0,
            "positions": positions if positions is not None else
            [{"symbol": "AAA US EQUITY", "quantity": "10", "avg_price": "100.00"}],
            "open_orders": open_orders if open_orders is not None else []}
    if version is None:
        del body["version"]
    body.update(override)
    return json.dumps(body).encode("utf-8")


def ok(body: bytes) -> HttpResponse:
    return HttpResponse(200, {"Content-Type": "application/json"}, body)


def account_adapter(steps=(), clock: Optional[SimulatedClock] = None, secrets=None, credential_name=None,
                    rate=100.0, capacity=100, seed: int = 7):
    clock = clock or SimulatedClock(T0)
    transport = ScriptedHttpTransport(steps)
    client = ReadOnlyHttpClient(transport, frozenset({ACCOUNT_PATH}), TokenBucket(rate, capacity, clock))
    adapter = RestAccountAdapter("broker_a", client, registry(), clock, "main",
                                 backoff=ExponentialBackoff(0.5, 2.0, 30.0, rng=random.Random(seed)),
                                 secrets=secrets, credential_name=credential_name)
    return adapter, transport, client, clock


def later(clock: SimulatedClock, seconds: float) -> datetime:
    return clock.now() + timedelta(seconds=seconds)
