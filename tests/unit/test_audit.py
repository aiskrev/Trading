"""AT-19: every record fully attributed; hash chain verifies; tampering detected; JSONL round-trip."""
import json

import pytest
from pydantic import ValidationError

from trading_agent.audit.ledger import AuditLedger
from trading_agent.contracts.audit import Attribution
from trading_agent.testing.harness import PASS_JSON, Harness, trade_json

ATTRIBUTION_FIELDS = ("model_version", "prompt_version", "strategy_version", "market_snapshot_id",
                      "risk_policy_version", "code_commit", "deployment_id", "broker_result")


def _run(tmp_path=None):
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    if tmp_path is not None:
        h.ledger = AuditLedger(tmp_path / "audit.jsonl")
        h.pipeline.ledger = h.ledger
    h.tick()
    return h


def test_attribution_has_exactly_the_eight_policy_fields():
    assert tuple(Attribution.model_fields) == ATTRIBUTION_FIELDS


def test_attribution_fields_are_mandatory_and_non_empty():
    good = {f: "x" for f in ATTRIBUTION_FIELDS}
    Attribution(**good)
    for f in ATTRIBUTION_FIELDS:
        with pytest.raises(ValidationError):
            Attribution(**{**good, f: ""})
        with pytest.raises(ValidationError):
            Attribution(**{k: v for k, v in good.items() if k != f})


def test_every_record_is_attributed_and_chain_verifies():
    h = _run()
    records = h.ledger.records
    assert {r.event_type.value for r in records} >= {"DECISION", "EXECUTION"}
    for r in records:
        for f in ATTRIBUTION_FIELDS:
            assert getattr(r.attribution, f)
    assert AuditLedger.verify(records)


def test_decision_is_written_before_execution():
    records = _run().ledger.records
    kinds = [r.event_type.value for r in records]
    assert kinds.index("DECISION") < kinds.index("EXECUTION")
    assert records[kinds.index("DECISION")].attribution.broker_result == "PENDING_SUBMIT"


def test_tampering_is_detected():
    records = _run().ledger.records
    tampered = list(records)
    tampered[0] = tampered[0].model_copy(update={"details": {"reason": "edited"}})
    assert not AuditLedger.verify(tampered)
    assert not AuditLedger.verify(records[1:])          # deletion
    assert not AuditLedger.verify(list(reversed(records)))  # reordering


def test_signature_is_never_written_to_audit(tmp_path):
    h = _run(tmp_path)
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"signature"' not in text
    loaded = AuditLedger.load(tmp_path / "audit.jsonl")
    assert AuditLedger.verify(loaded) and len(loaded) == len(h.ledger.records)
    assert json.loads(text.splitlines()[0])["seq"] == 0
