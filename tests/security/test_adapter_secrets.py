"""Credentials flow from SecretsProvider to the transport only: never into logs, issues, repr or records."""
import logging

from trading_agent.account_data.http import HttpResponse
from trading_agent.core.errors import AuthRejected, TransportError
from trading_agent.testing.feeds import EphemeralTestSecrets, account_adapter, account_body, feed, ok, quote_msg
from trading_agent.testing.harness import T0


def test_stream_token_reaches_transport_but_nowhere_else(caplog):
    secrets = EphemeralTestSecrets(["MD_TOKEN"])
    token = secrets.raw("MD_TOKEN")
    with caplog.at_level(logging.DEBUG):
        adapter, transport, clock = feed(sessions=[[quote_msg(1, T0), TransportError("reset")], []],
                                         connect_errors=[None, AuthRejected("401")],
                                         secrets=secrets, credential_name="MD_TOKEN")
        batches = [adapter.poll() for _ in range(3)]
    assert transport.last_token_matches(token) and all(transport.token_was_supplied)
    surfaces = [caplog.text, repr(adapter), adapter.health().model_dump_json()]
    surfaces += [i.model_dump_json() for b in batches for i in b.issues]
    surfaces += [r.model_dump_json() for b in batches for r in b.records]
    assert not any(token in s for s in surfaces)


def test_account_token_reaches_transport_but_nowhere_else(caplog):
    secrets = EphemeralTestSecrets(["ACCT_TOKEN"])
    token = secrets.raw("ACCT_TOKEN")
    with caplog.at_level(logging.DEBUG):
        adapter, transport, _, clock = account_adapter(secrets=secrets, credential_name="ACCT_TOKEN")
        transport.push(ok(account_body(clock.now())))
        good = adapter.fetch()
        clock.advance(1)
        transport.push(HttpResponse(401, {}, b""))
        bad = adapter.fetch()
    assert transport.last_auth_matches("Bearer " + token)
    surfaces = [caplog.text, repr(adapter), good.record.model_dump_json()] + [i.model_dump_json() for i in bad.issues]
    assert not any(token in s for s in surfaces)


def test_account_record_has_no_account_number_field():
    from trading_agent.account_data.records import AccountRecord
    assert not any("number" in f or "account_id" in f for f in AccountRecord.model_fields)
