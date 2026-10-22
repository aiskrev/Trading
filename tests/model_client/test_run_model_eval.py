"""scripts/run_model_eval.py with a fake SDK: confirmation, resume, recordings, provenance, failures, no holdout."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_anthropic_transport import (NO_TRADE, MODEL, InternalServerError, Secrets, fake_sdk, message)

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("run_model_eval", ROOT / "scripts" / "run_model_eval.py")
rme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rme)
ev = rme.ev


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "RECORDINGS", tmp_path / "recordings")
    monkeypatch.setattr(ev, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr("trading_agent.model_client.client.time.sleep", lambda s: None)
    return tmp_path


def run(sandbox, args, sdk, answer="YES", secrets=Secrets):
    lines = []
    code = rme.main(args, prompt=lambda p: "unused", confirm=lambda p: answer, out=lines.append,
                    secrets_factory=lambda: secrets(), sdk=sdk, out_dir=sandbox / "runs")
    return code, "\n".join(lines)


def test_a_smoke_run_sends_the_frozen_requests_and_records_the_outputs(sandbox):
    sdk = fake_sdk(message())
    code, text = run(sandbox, ["trading_planner.v1", "--split", "development", "--limit", "2"], sdk)
    assert code == 0 and "Upper bound on cost" in text and "[2/2]" in text and "imported 2 outputs" in text
    assert len(sdk.calls) == 2
    spec_v1 = ev.prompt_for("trading_planner.v1")
    for call in sdk.calls:
        assert call["model"] == MODEL and call["system"] == spec_v1.text
        assert call["output_config"] == {"effort": "medium"} and "tools" not in call
        payload = json.loads(call["messages"][0]["content"])
        assert "evaluation_time" in payload and "output_schema" in payload and "market_snapshot" in payload
    recorded = json.loads((sandbox / "recordings" / "trading_planner.v1.json").read_text(encoding="utf-8"))
    assert len(recorded) == 2 and set(recorded.values()) == {NO_TRADE}
    prov = [json.loads(l) for l in (sandbox / "recordings" / "trading_planner.v1.provenance.jsonl").read_text().splitlines()]
    assert prov[0]["model"] == MODEL and prov[0]["effort"] == "medium" and prov[0]["imported"] == 2
    assert prov[0]["set_version"] == "planner-eval.v2" and "graded 2/153" in text


def test_a_second_run_continues_with_new_requests_and_never_rewrites_recordings(sandbox):
    sdk = fake_sdk(message())
    run(sandbox, ["trading_planner.v1", "--limit", "2"], sdk)
    first = json.loads((sandbox / "recordings" / "trading_planner.v1.json").read_text(encoding="utf-8"))
    run(sandbox, ["trading_planner.v1", "--limit", "2"], sdk)
    second = json.loads((sandbox / "recordings" / "trading_planner.v1.json").read_text(encoding="utf-8"))
    assert len(sdk.calls) == 4 and len(second) == 4 and all(second[k] == v for k, v in first.items())
    sent = [c["messages"][0]["content"] for c in sdk.calls]
    assert len(set(sent)) == 4                                      # four different scenarios, none repeated


def test_without_a_yes_nothing_is_sent(sandbox):
    sdk = fake_sdk(message())
    code, text = run(sandbox, ["trading_planner.v1", "--limit", "3"], sdk, answer="yes")
    assert code == 1 and "cancelled" in text and sdk.calls == [] and not (sandbox / "recordings").exists()


def test_a_scope_flag_is_required_and_the_holdout_is_refused(sandbox):
    for args in (["trading_planner.v1"], ["trading_planner.v1", "--split", "holdout", "--all"]):
        with pytest.raises(SystemExit):
            run(sandbox, args, fake_sdk(message()))


def test_calls_without_usable_output_are_reported_not_hidden(sandbox):
    sdk = fake_sdk(InternalServerError(529))
    code, text = run(sandbox, ["trading_planner.v1", "--limit", "2"], sdk)
    assert code == 0 and "no usable output" in text and "graded 0/153" in text
    failures = list((sandbox / "runs").glob("*.failures.json"))
    assert len(failures) == 1 and len(json.loads(failures[0].read_text(encoding="utf-8"))) == 2


def test_the_upper_bound_grows_with_requests_and_output_budget():
    one = [{"payload": {"a": "x" * 3000}, "system": "s" * 3000}]
    assert rme.upper_bound_usd(one * 2, 6000) == pytest.approx(2 * rme.upper_bound_usd(one, 6000))
    assert rme.upper_bound_usd(one, 6000) > rme.upper_bound_usd(one, 3000) > 0


def test_without_a_vault_token_the_script_stops_before_any_call(sandbox, monkeypatch):
    monkeypatch.delenv("TRADING_VAULT_TOKEN", raising=False)
    sdk, lines = fake_sdk(message()), []
    code = rme.main(["trading_planner.v1", "--limit", "1"], prompt=lambda p: "x", confirm=lambda p: "YES",
                    out=lines.append, sdk=sdk, out_dir=sandbox / "runs")
    assert code == 1 and "TRADING_VAULT_TOKEN" in "\n".join(lines) and sdk.calls == []


def test_the_script_never_prints_or_stores_the_key(sandbox):
    sdk = fake_sdk(message())
    _, text = run(sandbox, ["trading_planner.v1", "--limit", "1"], sdk)
    blob = text + "".join(p.read_text(encoding="utf-8") for p in sandbox.rglob("*") if p.is_file())
    assert SimpleNamespace(**{"k": "sk-" + "ant-FAKE-KEY-0123456789abcdef"}).k not in blob
