"""Roadmap P2 acceptance 1, 2, 3 (log part) and 5 for the model client, against a fake SDK transport.

No network, no SDK, no key: the fake transport stands in for the real one, which is a separate step.
"""
import json
import logging
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_agent.contracts.account import AccountSnapshot
from trading_agent.contracts.features import FeatureSet
from trading_agent.core.errors import LLMUnavailable, SchemaFailure
from trading_agent.core.types import IdFactory
from trading_agent.model_client.client import ModelVersionMismatch, RetryPolicy, SdkLLMClient
from trading_agent.model_client.transport import (ContentBlock, ModelReply, ModelRequest, TransportAuthError,
                                                  TransportBadRequest, TransportRateLimited, TransportServerError,
                                                  TransportTimeout)
from trading_agent.planning.planner import ClaudePlanner
from trading_agent.planning.prompts import PromptSpec
from trading_agent.planning.recorded_llm import RecordedLLM, RecordingLLM, request_key
from trading_agent.testing.harness import T0, market

MODEL = "claude-sonnet-5-5"
SYSTEM = "SYSTEM-MARKER-7f3a: you are TRADING_PLANNER"
PAYLOAD = {"b": 2, "a": {"z": 1, "y": "PAYLOAD-MARKER-91c2"}}
NO_TRADE = json.dumps({"decision": "NO_TRADE", "public_rationale": "nothing to do"})


def text_reply(text=NO_TRADE, model=MODEL, stop="end_turn"):
    return ModelReply(model=model, blocks=[ContentBlock(type="text", text=text)], stop_reason=stop,
                      input_tokens=10, output_tokens=5)


class FakeSdk:
    """Records every request exactly as a real transport would receive it; plays back a script."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step


class Time:
    """Deterministic clock and sleep, so retry/deadline behaviour is exact."""

    def __init__(self):
        self.now, self.slept = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


def client(sdk, time=None, **retry):
    time = time or Time()
    return SdkLLMClient(sdk, MODEL, retry=RetryPolicy(**retry) if retry else RetryPolicy(), sleep=time.sleep,
                        monotonic=time.monotonic, jitter=lambda: 0.5), time


def ask(c):
    return c.complete(SYSTEM, "trading_planner", "v1", PAYLOAD)


# ------------------------------------------------------------------ 1. no tool is ever offered
def test_request_has_no_field_through_which_a_tool_could_be_offered():
    assert set(ModelRequest.model_fields) == {"model", "system", "user", "max_tokens", "timeout_s", "effort"}
    for extra in ({"tools": []}, {"tool_choice": {"type": "auto"}}, {"mcp_servers": []}, {"allowed_tools": ["Bash"]}):
        with pytest.raises(ValidationError):
            ModelRequest(model=MODEL, system="s", user="u", max_tokens=10, timeout_s=1, **extra)


def test_every_request_sent_is_tools_free_and_carries_the_pinned_model():
    sdk = FakeSdk(TransportServerError(), text_reply())
    c, _ = client(sdk, max_attempts=2)
    ask(c)
    assert len(sdk.requests) == 2
    for r in sdk.requests:
        sent = r.model_dump()
        assert set(sent) == {"model", "system", "user", "max_tokens", "timeout_s", "effort"}
        assert sent["effort"] == "medium" and sent["max_tokens"] == 6000
        assert sent["model"] == MODEL and sent["system"] == SYSTEM
        assert not any("tool" in k for k in sent)


def test_user_message_is_canonical_json_of_the_payload_whatever_the_key_order():
    sdk = FakeSdk(text_reply())
    c, _ = client(sdk)
    c.complete(SYSTEM, "p", "v1", {"b": 2, "a": {"z": 1, "y": "PAYLOAD-MARKER-91c2"}})
    c.complete(SYSTEM, "p", "v1", {"a": {"y": "PAYLOAD-MARKER-91c2", "z": 1}, "b": 2})
    assert sdk.requests[0].user == sdk.requests[1].user == '{"a":{"y":"PAYLOAD-MARKER-91c2","z":1},"b":2}'


def test_aliases_are_refused():
    for bad in ("", "claude-sonnet-latest"):
        with pytest.raises(ValueError):
            SdkLLMClient(FakeSdk(text_reply()), bad)


# ------------------------------------------------------------------ 1b. a tool-use (or other) block is a schema failure
@pytest.mark.parametrize("blocks,stop,code", [
    ([ContentBlock(type="tool_use")], "tool_use", "NON_TEXT_BLOCK:tool_use"),
    ([ContentBlock(type="text", text=NO_TRADE), ContentBlock(type="tool_use")], "end_turn", "NON_TEXT_BLOCK:tool_use"),
    ([ContentBlock(type="thinking")], "end_turn", "EMPTY_REPLY"),                     # reasoning only, no answer
    ([ContentBlock(type="redacted_thinking"), ContentBlock(type="tool_use")], "tool_use", "NON_TEXT_BLOCK:tool_use"),
    ([ContentBlock(type="server_tool_use")], "end_turn", "NON_TEXT_BLOCK:server_tool_use"),
    ([], "end_turn", "EMPTY_REPLY"),
    ([ContentBlock(type="text", text="{}"), ContentBlock(type="text", text="{}")], "end_turn", "MULTIPLE_BLOCKS"),
    ([ContentBlock(type="text", text=None)], "end_turn", "EMPTY_TEXT"),
    ([ContentBlock(type="text", text="  \n")], "end_turn", "EMPTY_TEXT"),
    ([ContentBlock(type="text", text=NO_TRADE)], "max_tokens", "INCOMPLETE_REPLY:max_tokens"),
    ([ContentBlock(type="text", text=NO_TRADE)], "refusal", "INCOMPLETE_REPLY:refusal"),
    ([ContentBlock(type="text", text=NO_TRADE)], None, "INCOMPLETE_REPLY:None"),
])
def test_anything_but_one_complete_text_block_is_a_schema_failure(blocks, stop, code):
    sdk = FakeSdk(ModelReply(model=MODEL, blocks=blocks, stop_reason=stop))
    c, time = client(sdk)
    with pytest.raises(SchemaFailure, match=code):
        ask(c)
    assert len(sdk.requests) == 1 and time.slept == []          # not retried: it would be paid for twice


def test_reasoning_blocks_are_ignored_but_the_answer_is_still_exactly_one_text_block():
    blocks = [ContentBlock(type="thinking"), ContentBlock(type="redacted_thinking"),
              ContentBlock(type="text", text=NO_TRADE)]
    c, _ = client(FakeSdk(ModelReply(model=MODEL, blocks=blocks, stop_reason="end_turn")))
    assert ask(c) == NO_TRADE
    two = blocks + [ContentBlock(type="text", text=NO_TRADE)]
    c, _ = client(FakeSdk(ModelReply(model=MODEL, blocks=two, stop_reason="end_turn")))
    with pytest.raises(SchemaFailure, match="MULTIPLE_BLOCKS"):
        ask(c)


def test_a_different_answering_model_is_refused():
    c, _ = client(FakeSdk(text_reply(model="claude-haiku-5-5")))
    with pytest.raises(ModelVersionMismatch):
        ask(c)


# ------------------------------------------------------------------ 2. transport failures -> fail-closed codes
@pytest.mark.parametrize("exc,code", [(TransportTimeout(), "TIMEOUT"), (TransportRateLimited(), "RATE_LIMITED"),
                                      (TransportServerError(), "SERVER_ERROR")])
def test_transient_failures_are_retried_once_then_unavailable(exc, code):
    sdk = FakeSdk(exc)
    c, time = client(sdk)
    with pytest.raises(LLMUnavailable, match=code):
        ask(c)
    assert len(sdk.requests) == 2 and len(time.slept) == 1


@pytest.mark.parametrize("exc,code", [(TransportAuthError(), "AUTH_ERROR"), (TransportBadRequest(), "BAD_REQUEST")])
def test_permanent_failures_are_not_retried(exc, code):
    sdk = FakeSdk(exc)
    c, time = client(sdk)
    with pytest.raises(LLMUnavailable, match=code):
        ask(c)
    assert len(sdk.requests) == 1 and time.slept == []


def test_a_retry_that_succeeds_returns_the_text():
    sdk = FakeSdk(TransportTimeout(), text_reply())
    c, time = client(sdk)
    assert ask(c) == NO_TRADE
    assert time.slept == [0.5]                                  # base 0.5 s x (0.5 + jitter 0.5)


def test_rate_limit_retry_after_is_honoured_but_capped():
    for retry_after, slept in ((2.0, 2.0), (60.0, 4.0)):
        sdk = FakeSdk(TransportRateLimited(retry_after_s=retry_after), text_reply())
        c, time = client(sdk, total_deadline_s=30)
        ask(c)
        assert time.slept == [slept]


def test_the_total_deadline_bounds_retries_and_each_attempt_timeout():
    sdk = FakeSdk(TransportTimeout(), text_reply())
    c, time = client(sdk, attempt_timeout_s=10, total_deadline_s=15, backoff_base_s=20, backoff_cap_s=20)
    with pytest.raises(LLMUnavailable, match="TIMEOUT"):        # the backoff alone would pass the deadline
        ask(c)
    assert len(sdk.requests) == 1 and time.slept == []
    sdk2 = FakeSdk(text_reply())
    c2, time2 = client(sdk2, attempt_timeout_s=10, total_deadline_s=4)
    ask(c2)
    assert sdk2.requests[0].timeout_s == 4                      # never longer than the time that is left


def test_an_exhausted_deadline_means_no_call_at_all():
    sdk = FakeSdk(text_reply())
    time = Time()
    time.now = 100.0
    c = SdkLLMClient(sdk, MODEL, retry=RetryPolicy(total_deadline_s=0.0001), sleep=time.sleep,
                     monotonic=iter([100.0, 200.0, 200.0, 200.0]).__next__)
    with pytest.raises(LLMUnavailable, match="DEADLINE"):
        ask(c)
    assert sdk.requests == []


# ------------------------------------------------------------------ the planner turns each failure into NO_TRADE
def _planner(sdk):
    c, _ = client(sdk)
    prompt = PromptSpec("trading_planner", "v1", SYSTEM, "0" * 64)
    return ClaudePlanner(c, prompt, "mean-reversion-demo.v1", {"name": "t"}, ["AAA"], IdFactory("t"))


def _inputs():
    m = market()
    a = AccountSnapshot.build(as_of=T0, cash=Decimal(100000), positions={}, open_orders=[],
                              realized_pnl_today=Decimal(0), orders_today=0, equity_high_water=Decimal(100000))
    f = FeatureSet(snapshot_id=m.snapshot_id, feature_version="features.v1",
                   values={"AAA": {"dev_from_sma_10_bps": Decimal(-40)}})
    return m, a, f


@pytest.mark.parametrize("script,failure", [
    ([TransportTimeout()], "LLM_UNAVAILABLE"),
    ([TransportRateLimited()], "LLM_UNAVAILABLE"),
    ([TransportServerError()], "LLM_UNAVAILABLE"),
    ([TransportAuthError()], "LLM_UNAVAILABLE"),
    ([ModelReply(model=MODEL, blocks=[ContentBlock(type="tool_use")], stop_reason="tool_use")], "SCHEMA_FAILURE"),
    ([ModelReply(model=MODEL, blocks=[], stop_reason="end_turn")], "SCHEMA_FAILURE"),
    ([text_reply("```json\n" + NO_TRADE + "\n```")], "SCHEMA_FAILURE"),          # parsed strictly downstream
    ([text_reply('{"decision": "TRADE", "symbol": "AAA"}')], "SCHEMA_FAILURE"),
    ([text_reply(model="some-other-model")], "MODEL_FAILURE"),
])
def test_planner_gets_no_trade_with_the_right_code(script, failure):
    result = _planner(FakeSdk(*script)).plan(*_inputs())
    assert result.failure == failure
    assert result.proposal.decision.value == "NO_TRADE" and result.proposal.reason_code == failure


def test_a_good_reply_goes_through_the_planner_with_the_pinned_model_in_attribution():
    result = _planner(FakeSdk(text_reply())).plan(*_inputs())
    assert result.failure is None and result.proposal.model_version == MODEL
    assert result.raw_output == NO_TRADE


# ------------------------------------------------------------------ 3. nothing but metadata is logged
def test_logs_carry_no_prompt_payload_or_reply(caplog):
    secret_reply = NO_TRADE.replace("nothing to do", "REPLY-MARKER-55d0")
    sdk = FakeSdk(TransportTimeout(), text_reply(secret_reply))
    c, _ = client(sdk)
    with caplog.at_level(logging.DEBUG, logger="trading_agent.model_client"):
        ask(c)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "model_call prompt=trading_planner.v1" in text and "outcome=TIMEOUT" in text and "outcome=OK" in text
    for marker in ("SYSTEM-MARKER-7f3a", "PAYLOAD-MARKER-91c2", "REPLY-MARKER-55d0"):
        assert marker not in text


# ------------------------------------------------------------------ 5. record / replay is byte-identical
def test_replay_of_recorded_responses_is_byte_identical():
    odd = NO_TRADE.replace("nothing to do", "Ünïcode ✓   trailing") + "  \n"
    live, _ = client(FakeSdk(text_reply(odd)))
    recorder = RecordingLLM(live)
    first = recorder.complete(SYSTEM, "trading_planner", "v1", PAYLOAD)
    replay = RecordedLLM(json.loads(json.dumps(recorder.recordings)), live.model_version)   # through a file
    again = replay.complete(SYSTEM, "trading_planner", "v1", PAYLOAD)
    assert first == odd == again and first.encode("utf-8") == again.encode("utf-8")
    assert list(recorder.recordings) == [request_key("trading_planner", "v1", SYSTEM, PAYLOAD)]
    with pytest.raises(LLMUnavailable):                      # an unseen request is a failure, never a guess
        replay.complete(SYSTEM, "trading_planner", "v1", {"other": 1})
