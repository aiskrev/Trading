"""The Anthropic Messages transport against a fake SDK module: what is sent, what is mapped, what never leaks."""
import json
import logging
import pickle
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from trading_agent.core.errors import LLMUnavailable, SchemaFailure, SecretUnavailable
from trading_agent.model_client.anthropic_transport import API_KEY_NAME, AnthropicMessagesTransport
from trading_agent.model_client.client import RetryPolicy, SdkLLMClient
from trading_agent.model_client.transport import (ModelRequest, TransportAuthError, TransportBadRequest,
                                                  TransportConnectionError, TransportRateLimited,
                                                  TransportServerError, TransportTimeout)

KEY = "sk-" + "ant-FAKE-KEY-0123456789abcdef"   # built at runtime: the secret scanner must not see a key literal
MODEL = "claude-sonnet-5-5"
NO_TRADE = json.dumps({"decision": "NO_TRADE", "public_rationale": "nothing to do"})


class APIConnectionError(Exception):
    pass


class APITimeoutError(APIConnectionError):
    pass


class APIStatusError(Exception):
    def __init__(self, status_code=400, retry_after=None):
        super().__init__("body with request details")
        self.status_code = status_code
        self.response = SimpleNamespace(headers={"retry-after": retry_after} if retry_after else {})


class RateLimitError(APIStatusError):
    pass


class AuthenticationError(APIStatusError):
    pass


class PermissionDeniedError(APIStatusError):
    pass


class BadRequestError(APIStatusError):
    pass


class InternalServerError(APIStatusError):
    pass


class FakeMessages:
    def __init__(self, outcome, calls):
        self._outcome, self._calls = outcome, calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeClient:
    def __init__(self, outcome, calls, options):
        self._outcome, self._calls, self._options = outcome, calls, options

    def with_options(self, **kwargs):
        self._options.append(kwargs)
        return SimpleNamespace(messages=FakeMessages(self._outcome, self._calls))


def fake_sdk(outcome):
    sdk = SimpleNamespace(calls=[], options=[], constructed=[], APIConnectionError=APIConnectionError,
                          APITimeoutError=APITimeoutError, APIStatusError=APIStatusError,
                          RateLimitError=RateLimitError, AuthenticationError=AuthenticationError,
                          PermissionDeniedError=PermissionDeniedError, BadRequestError=BadRequestError,
                          InternalServerError=InternalServerError)

    def anthropic(**kwargs):
        sdk.constructed.append(kwargs)
        return FakeClient(outcome, sdk.calls, sdk.options)
    sdk.Anthropic = anthropic
    return sdk


def message(blocks=None, model=MODEL, stop="end_turn"):
    blocks = blocks if blocks is not None else [SimpleNamespace(type="text", text=NO_TRADE)]
    return SimpleNamespace(model=model, content=blocks, stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=1200, output_tokens=80))


class Secrets:
    def __init__(self, present=True):
        self.present, self.asked = present, []

    def get(self, name):
        self.asked.append(name)
        if not self.present:
            raise SecretUnavailable("nope")
        return SecretStr(KEY)


def request(**kw):
    return ModelRequest(model=MODEL, system="SYS", user='{"a":1}', max_tokens=6000, timeout_s=7.5, **kw)


# ------------------------------------------------------------------ what is sent
def test_the_sdk_call_has_exactly_the_closed_set_of_arguments_and_no_tool():
    sdk = fake_sdk(message())
    reply = AnthropicMessagesTransport(Secrets(), sdk=sdk).send(request())
    assert sdk.calls == [dict(model=MODEL, max_tokens=6000, system="SYS",
                              messages=[{"role": "user", "content": '{"a":1}'}], output_config={"effort": "medium"})]
    forbidden = {"tools", "tool_choice", "mcp_servers", "temperature", "top_p", "top_k", "thinking", "betas",
                 "fallbacks", "extra_body", "extra_headers"}
    assert not forbidden & set(sdk.calls[0])
    assert sdk.options == [{"timeout": 7.5}]
    assert reply.model == MODEL and reply.stop_reason == "end_turn" and reply.input_tokens == 1200


def test_the_key_comes_from_the_secrets_provider_and_sdk_retries_are_off():
    secrets, sdk = Secrets(), fake_sdk(message())
    transport = AnthropicMessagesTransport(secrets, sdk=sdk)
    transport.send(request())
    transport.send(request())
    assert secrets.asked == [API_KEY_NAME]                        # read once, by logical name
    assert sdk.constructed == [{"api_key": KEY, "max_retries": 0}]  # no base_url, no SDK-level retry


def test_a_missing_key_is_an_auth_failure_without_a_network_call():
    sdk = fake_sdk(message())
    with pytest.raises(TransportAuthError):
        AnthropicMessagesTransport(Secrets(present=False), sdk=sdk).send(request())
    assert sdk.constructed == [] and sdk.calls == []


def test_the_key_never_appears_in_repr_errors_or_pickles():
    transport = AnthropicMessagesTransport(Secrets(), sdk=fake_sdk(AuthenticationError(401)))
    assert KEY not in repr(transport)
    with pytest.raises(TransportAuthError) as info:
        transport.send(request())
    assert KEY not in str(info.value) and "request details" not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    with pytest.raises(TypeError):
        pickle.dumps(transport)


# ------------------------------------------------------------------ SDK errors -> transport errors
@pytest.mark.parametrize("raised,expected", [
    (APITimeoutError(), TransportTimeout),
    (APIConnectionError(), TransportConnectionError),
    (RateLimitError(429, "3"), TransportRateLimited),
    (AuthenticationError(401), TransportAuthError),
    (PermissionDeniedError(403), TransportAuthError),
    (InternalServerError(500), TransportServerError),
    (InternalServerError(529), TransportServerError),
    (APIStatusError(503), TransportServerError),
    (APIStatusError(429), TransportRateLimited),
    (BadRequestError(400), TransportBadRequest),
    (APIStatusError(404), TransportBadRequest),
])
def test_every_sdk_error_maps_to_a_code_only_transport_error(raised, expected):
    transport = AnthropicMessagesTransport(Secrets(), sdk=fake_sdk(raised))
    with pytest.raises(expected) as info:
        transport.send(request())
    assert type(info.value) is expected and "request details" not in str(info.value)


def test_retry_after_header_is_carried_when_numeric_and_ignored_otherwise():
    for header, expected in (("3", 3.0), ("soon", None), (None, None)):
        with pytest.raises(TransportRateLimited) as info:
            AnthropicMessagesTransport(Secrets(), sdk=fake_sdk(RateLimitError(429, header))).send(request())
        assert info.value.retry_after_s == expected


# ------------------------------------------------------------------ reply mapping
def test_thinking_text_is_never_copied_and_other_blocks_keep_only_their_type():
    blocks = [SimpleNamespace(type="thinking", thinking="PRIVATE REASONING", text="PRIVATE"),
              SimpleNamespace(type="text", text=NO_TRADE), SimpleNamespace(type="tool_use", name="x", input={})]
    reply = AnthropicMessagesTransport(Secrets(), sdk=fake_sdk(message(blocks))).send(request())
    assert [(b.type, b.text) for b in reply.blocks] == [("thinking", None), ("text", NO_TRADE), ("tool_use", None)]
    assert "PRIVATE" not in reply.model_dump_json()


# ------------------------------------------------------------------ end to end through SdkLLMClient
def llm(outcome):
    sdk = fake_sdk(outcome)
    client = SdkLLMClient(AnthropicMessagesTransport(Secrets(), sdk=sdk), MODEL, retry=RetryPolicy(),
                          sleep=lambda s: None, monotonic=lambda: 0.0, jitter=lambda: 0.5)
    return client, sdk


def test_a_reply_with_thinking_and_one_text_block_is_accepted_byte_for_byte():
    reply = message([SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=NO_TRADE + "\n")])
    client, _ = llm(reply)
    assert client.complete("S", "trading_planner", "v1", {"x": 1}) == NO_TRADE + "\n"


def test_tool_use_refusal_truncation_or_no_text_fail_closed_through_the_real_mapping():
    for outcome in (message([SimpleNamespace(type="tool_use")], stop="tool_use"), message(stop="refusal"),
                    message(stop="max_tokens"), message([SimpleNamespace(type="thinking", thinking="x")])):
        client, _ = llm(outcome)
        with pytest.raises(SchemaFailure):
            client.complete("S", "p", "v1", {})


def test_transient_errors_are_retried_once_and_auth_is_not():
    client, sdk = llm(InternalServerError(529))
    with pytest.raises(LLMUnavailable, match="SERVER_ERROR"):
        client.complete("S", "p", "v1", {})
    assert len(sdk.calls) == 2
    client, sdk = llm(AuthenticationError(401))
    with pytest.raises(LLMUnavailable, match="AUTH_ERROR"):
        client.complete("S", "p", "v1", {})
    assert len(sdk.calls) == 1


def test_the_key_is_not_logged(caplog):
    client, _ = llm(message())
    with caplog.at_level(logging.DEBUG):
        client.complete("S", "p", "v1", {})
    assert KEY not in "\n".join(r.getMessage() for r in caplog.records)
