"""Malformed-message handling: every bad payload is rejected with a code and no payload echo."""
import json

import pytest

from trading_agent.contracts.data_quality import DataIssueCode
from trading_agent.core.errors import AuthRejected, RateLimited
from trading_agent.market_data.decoders import GenericJsonQuoteDecoder
from trading_agent.market_data.wire import DecodeError
from trading_agent.testing.feeds import heartbeat, quote_msg, registry
from trading_agent.testing.harness import T0

DEC = GenericJsonQuoteDecoder("vendor_a", registry(), max_bytes=4096)
M = DataIssueCode.MALFORMED_MESSAGE


def good():
    return json.loads(quote_msg(1, T0))


def enc(obj):
    return json.dumps(obj).encode()


@pytest.mark.parametrize("raw,code", [
    (b"", M),
    (b"not json", M),
    (b"\xff\xfe\x00", M),                                      # invalid utf-8
    (b"[1,2,3]", M),                                           # not an object
    (b"{}", M),                                                # no type
    (enc({**good(), "type": "trade"}), M),                     # unknown type
    (enc({**good(), "type": 5}), M),
    (enc({**good(), "extra": 1}), M),                          # unexpected field
    (enc({k: v for k, v in good().items() if k != "bid"}), M), # missing field
    (enc({**good(), "bid": "abc"}), M),
    (enc({**good(), "bid": "-1"}), M),                         # non-positive price
    (enc({**good(), "bid_size": "-5"}), M),
    (enc({**good(), "seq": -1}), M),
    (enc({**good(), "ts": "yesterday"}), M),
    (enc({**good(), "ts": "2026-01-05T14:30:00"}), M),         # naive timestamp
    (quote_msg(1, T0, symbol="ZZZ.US"), DataIssueCode.UNKNOWN_INSTRUMENT),
    (b'{"type":"quote","type":"quote"}', M),                   # duplicate keys
    (b'{"type":"quote","seq":1,"symbol":"AAA.US","ts":"2026-01-05T14:30:00+00:00","bid":NaN,'
     b'"ask":"1","bid_size":"1","ask_size":"1","last":"1"}', M),
    (b"{" + b" " * 5000 + b"}", M),                            # oversize
    (enc({"type": "error", "code": "lower-case!"}), M),
])
def test_malformed_messages_rejected(raw, code):
    with pytest.raises(DecodeError) as info:
        DEC.decode(raw, T0)
    assert info.value.code is code
    assert len(info.value.detail) <= 200


def test_detail_never_echoes_payload():
    marker = "PAYLOAD-MARKER-123"
    with pytest.raises(DecodeError) as info:
        DEC.decode(enc({**good(), "bid": marker}), T0)
    assert marker not in str(info.value)


def test_valid_quote_and_heartbeat():
    d = DEC.decode(quote_msg(7, T0, symbol="xnas:aaa"), T0)
    assert d.kind == "quote" and d.seq == 7
    assert d.record.instrument == "AAA" and d.record.vendor_symbol == "xnas:aaa"
    assert d.record.source_id == "vendor_a" and d.record.source_ts == T0 and d.record.received_at == T0
    assert DEC.decode(heartbeat(8, T0), T0).kind == "heartbeat"


def test_vendor_error_messages():
    with pytest.raises(RateLimited) as rl:
        DEC.decode(enc({"type": "error", "code": "RATE_LIMIT", "retry_after_s": 7}), T0)
    assert rl.value.retry_after_s == 7
    with pytest.raises(AuthRejected):
        DEC.decode(enc({"type": "error", "code": "UNAUTHORIZED"}), T0)
    assert DEC.decode(enc({"type": "error", "code": "MAINTENANCE"}), T0).error_code == "MAINTENANCE"
