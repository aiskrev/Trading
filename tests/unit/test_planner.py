"""AT-06 (strict planner output handling) and AT-07 (evidence requirements)."""
import json

import pytest
from pydantic import ValidationError

from trading_agent.contracts.proposal import Decision, ProposalBody
from trading_agent.core.errors import LLMUnavailable, SchemaFailure
from trading_agent.planning.output_parser import parse_strict
from trading_agent.testing.harness import NO_TRADE_JSON, PASS_JSON, Harness, trade_json


@pytest.mark.parametrize("raw", [
    "",
    "NO_TRADE",
    "```json\n" + NO_TRADE_JSON + "\n```",
    "Here you go: " + NO_TRADE_JSON,
    NO_TRADE_JSON + " trailing",
    '{"decision": "NO_TRADE", "decision": "TRADE", "public_rationale": "x"}',
    '{"decision": "NO_TRADE", "public_rationale": "x", "unexpected": 1}',
    '{"decision": "TRADE", "symbol": "AAA", "side": "BUY", "order_type": "LIMIT", "limit_price": NaN, '
    '"requested_notional": 1, "invalidation_conditions": ["x"], "evidence_refs": ["quote:AAA"], "public_rationale": "x"}',
    "[" + NO_TRADE_JSON + "]",
    "{" + " " * 25000 + "}",
])
def test_parse_strict_rejects(raw):
    with pytest.raises(SchemaFailure):
        parse_strict(raw, ProposalBody)


def test_parse_strict_accepts_exact_json():
    assert parse_strict("  " + NO_TRADE_JSON + "\n", ProposalBody).decision is Decision.NO_TRADE


@pytest.mark.parametrize("raw", ["not json", "```json\n{}\n```", trade_json(order_type="MARKET")])
def test_bad_model_output_becomes_no_trade_and_is_audited(raw):
    h = Harness(planner_fn=lambda p: raw, critic_fn=lambda p: PASS_JSON)
    result = h.tick()
    assert result.proposal.decision is Decision.NO_TRADE
    assert "SCHEMA_FAILURE" in result.proposal.public_rationale
    assert result.order is None
    assert any(r.event_type.value == "SCHEMA_FAILURE" for r in h.ledger.records)


def test_llm_unavailable_becomes_no_trade():
    def boom(payload):
        raise LLMUnavailable("timeout")
    h = Harness(planner_fn=boom, critic_fn=lambda p: PASS_JSON)
    result = h.tick()
    assert result.proposal.decision is Decision.NO_TRADE and result.order is None
    assert "LLM_UNAVAILABLE" in result.proposal.public_rationale


def test_unexpected_planner_exception_becomes_no_trade():
    h = Harness(planner_fn=lambda p: 1 / 0, critic_fn=lambda p: PASS_JSON)
    assert h.tick().proposal.decision is Decision.NO_TRADE


def test_attribution_is_set_by_code_not_model():
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    result = h.tick()
    p = result.proposal
    assert p.market_snapshot_id == h.current.snapshot_id
    assert p.model_version == "scripted-planner"
    assert p.prompt_version == "trading_planner.v1" and len(p.prompt_sha256) == 64
    # A model that tries to supply attribution itself is a schema failure.
    sneaky = json.loads(trade_json())
    sneaky["model_version"] = "gpt-x"
    with pytest.raises(SchemaFailure):
        parse_strict(json.dumps(sneaky), ProposalBody)


def test_trade_requires_evidence_and_invalidation():
    base = json.loads(trade_json())
    for field in ("evidence_refs", "invalidation_conditions"):
        with pytest.raises(ValidationError):
            ProposalBody.model_validate({**base, field: []})
    with pytest.raises(ValidationError):
        ProposalBody.model_validate({**base, "symbol": None})


def test_no_trade_must_not_carry_trade_fields():
    with pytest.raises(ValidationError):
        ProposalBody.model_validate({"decision": "NO_TRADE", "symbol": "AAA", "public_rationale": "x"})


@pytest.mark.parametrize("refs,code", [
    (["quote:AAA", "news:does-not-exist"], "UNRESOLVED_EVIDENCE"),
    (["venue:status"], "MISSING_QUOTE_EVIDENCE"),
    (["quote:AAA", "position:AAA"], "UNRESOLVED_EVIDENCE"),  # no AAA position exists
])
def test_unresolvable_evidence_is_denied(refs, code):
    h = Harness(planner_fn=lambda p: trade_json(refs=refs), critic_fn=lambda p: PASS_JSON)
    result = h.tick()
    assert result.order is None
    assert code in result.decision.reason_codes


def test_payload_gives_model_valid_evidence_refs_and_schema():
    seen = {}

    def capture(payload):
        seen.update(payload)
        return NO_TRADE_JSON
    Harness(planner_fn=capture).tick()
    assert "quote:AAA" in seen["valid_evidence_refs"]
    assert seen["output_schema"]["additionalProperties"] is False
    assert seen["allowed_universe"] == ["AAA", "BBB"]
