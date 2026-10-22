"""Planner-quality eval harness: frozen set, chronological splits, sealed holdout, calibrated graders,
regression deltas, and the export/import round trip."""
import json
import shutil
from datetime import date

import pytest

from evals.planner_quality import run, scenarios as sc
from evals.planner_quality.reference_models import REFERENCE_MODELS, rule_follower
from evals.planner_quality.taxonomy import Failure, grade

DEV, VAL, HOLD = (sc.load(s) for s in ("development", "validation", "holdout"))


# -------------------------------------------------------------------- frozen set
def test_frozen_set_is_intact_and_reproducible():
    manifest = sc.freeze()  # raises if the generator no longer reproduces the frozen files
    assert manifest["total"] == len(DEV) + len(VAL) + len(HOLD) == 255


def test_tampered_scenarios_are_refused(tmp_path, monkeypatch):
    for f in (sc.SCENARIOS_FILE, sc.OUTPUT_SCHEMA_FILE, sc.MANIFEST_FILE):
        shutil.copy(f, tmp_path / f.name)
    tampered = tmp_path / sc.SCENARIOS_FILE.name
    tampered.write_bytes(tampered.read_bytes().replace(b'"NO_TRADE"', b'"TRADE"', 1))
    monkeypatch.setattr(sc, "FROZEN", tmp_path)
    monkeypatch.setattr(sc, "SCENARIOS_FILE", tampered)
    monkeypatch.setattr(sc, "OUTPUT_SCHEMA_FILE", tmp_path / sc.OUTPUT_SCHEMA_FILE.name)
    monkeypatch.setattr(sc, "MANIFEST_FILE", tmp_path / sc.MANIFEST_FILE.name)
    with pytest.raises(RuntimeError, match="modified"):
        sc.load()


# -------------------------------------------------------------------- splits
def _dates(rows):
    return [date.fromisoformat(r["date"]) for r in rows]


def test_splits_are_chronological_with_embargo():
    assert max(_dates(DEV)) < min(_dates(VAL)) and max(_dates(VAL)) < min(_dates(HOLD))
    assert (min(_dates(VAL)) - max(_dates(DEV))).days >= 7
    assert (min(_dates(HOLD)) - max(_dates(VAL))).days >= 7


def test_splits_are_disjoint_and_cover_every_kind():
    ids = [s["scenario_id"] for s in DEV + VAL + HOLD]
    assert len(ids) == len(set(ids))
    for rows in (DEV, VAL, HOLD):
        assert {s["kind"] for s in rows} == set(sc.KINDS)


# -------------------------------------------------------------------- holdout seal
def test_holdout_is_sealed(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "ACCESS_LOG", tmp_path / "access.log")
    for final in (False, True):
        with pytest.raises(run.HoldoutSealed):
            run.run_label("trading_planner.v1", "holdout", final=final)
        with pytest.raises(run.HoldoutSealed):
            run.export("trading_planner.v1", "holdout", final=final)
    assert not (tmp_path / "access.log").exists()


def test_holdout_access_is_logged_when_allowed(tmp_path, monkeypatch):
    cands = tmp_path / "c.yaml"
    cands.write_text("candidates: [calibration:rule_follower]\n", encoding="utf-8")
    monkeypatch.setattr(run, "CANDIDATES_FILE", cands)
    monkeypatch.setattr(run, "ACCESS_LOG", tmp_path / "access.log")
    run.run_label("calibration:rule_follower", "holdout", final=True, outputs_fn=rule_follower)
    entry = json.loads((tmp_path / "access.log").read_text(encoding="utf-8").splitlines()[0])
    assert entry["label"] == "calibration:rule_follower" and entry["action"] == "evaluate"


def test_holdout_has_never_been_accessed():
    assert not run.ACCESS_LOG.exists()


# -------------------------------------------------------------------- grader calibration
def _grades(model, rows):
    return {s["scenario_id"]: (s, grade(s, model(s["payload"]))) for s in rows}


@pytest.mark.parametrize("rows", [DEV, VAL], ids=["development", "validation"])
def test_rule_follower_with_the_clock_fails_nowhere(rows):
    for s, g in _grades(REFERENCE_MODELS["rule_follower"], rows).values():
        assert g.failures == [], (s["scenario_id"], g.failures)
        assert g.secondary == []


@pytest.mark.parametrize("rows", [DEV, VAL], ids=["development", "validation"])
def test_without_the_clock_stale_snapshots_cannot_be_caught(rows):
    """What evaluation_time buys: the same rule-follower, payload stripped of the clock, trades on old snapshots."""
    for s in rows:
        payload = {k: v for k, v in s["payload"].items() if k != "evaluation_time"}
        g = grade(s, REFERENCE_MODELS["rule_follower"](payload))
        assert (Failure.TRADE_ON_STALE_DATA in g.failures) == s["requires_clock"], s["scenario_id"]


def test_every_scenario_carries_the_clock_and_only_stale_snapshots_are_hours_old():
    from datetime import datetime
    for s in DEV + VAL + HOLD:
        p = s["payload"]
        evaluated = datetime.fromisoformat(p["evaluation_time"].replace("Z", "+00:00"))
        age = (evaluated - datetime.fromisoformat(p["market_snapshot"]["as_of"].replace("Z", "+00:00"))).total_seconds()
        assert (age > 3600) == (s["kind"] == "stale_snapshot") and s["requires_clock"] == (s["kind"] == "stale_snapshot")
        assert age >= 0.1 - 1e-9


@pytest.mark.parametrize("model,kinds,failure", [
    ("naive_trader", {"duplicate_open_order"}, Failure.DUPLICATE_ORDER),
    ("naive_trader", {"instrument_not_trading"}, Failure.INCORRECT_MARKET_STATUS),
    ("naive_trader", {"stale_quote", "stale_snapshot"}, Failure.TRADE_ON_STALE_DATA),
    ("naive_trader", {"loss_limit", "wide_spread"}, Failure.RISK_SEEKING),
    ("injection_obeyer", {"injection_no_signal", "injection_with_signal"}, Failure.PROMPT_INJECTION),
    ("chatty", set(sc.KINDS), Failure.SCHEMA_FAILURE),
    ("hallucinator", set(sc.KINDS), Failure.UNSUPPORTED_FACTUAL_CLAIM),
])
@pytest.mark.parametrize("rows", [DEV, VAL], ids=["development", "validation"])
def test_known_flaws_are_always_caught(rows, model, kinds, failure):
    graded = [g for s, g in _grades(REFERENCE_MODELS[model], rows).values() if s["kind"] in kinds]
    assert graded and all(failure in g.failures for g in graded)


@pytest.mark.parametrize("raw,failure", [
    ("not json", Failure.SCHEMA_FAILURE),
    ('{"decision":"NO_TRADE","public_rationale":"x","extra":1}', Failure.SCHEMA_FAILURE),
])
def test_schema_grader(raw, failure):
    assert grade(DEV[0], raw).failures == [failure]


def test_not_run_is_not_a_failure():
    g = grade(DEV[0], None)
    assert g.status == "NOT_RUN" and g.failures == []


# -------------------------------------------------------------------- regression report
def test_regression_deltas_and_new_failures():
    base = run.run_label("calibration:rule_follower", "validation", outputs_fn=REFERENCE_MODELS["rule_follower"])
    new = run.run_label("calibration:naive_trader", "validation", outputs_fn=REFERENCE_MODELS["naive_trader"])
    cmp = run.compare(new, base)
    assert cmp["regressed"] and cmp["new_failures"]
    assert cmp["rate_deltas"]["DUPLICATE_ORDER"] > 0
    assert all(f["failure"] != "SCHEMA_FAILURE" for f in cmp["new_failures"])
    same = run.compare(base, base)
    assert same["new_failures"] == [] and not same["regressed"] and set(same["rate_deltas"].values()) == {0}
    assert "delta vs baseline" in run.render(new, cmp)


def test_compare_refuses_different_splits():
    a = run.run_label("calibration:rule_follower", "validation", outputs_fn=rule_follower)
    b = run.run_label("calibration:rule_follower", "development", outputs_fn=rule_follower)
    with pytest.raises(ValueError):
        run.compare(a, b)


# -------------------------------------------------------------------- export / import round trip
def test_export_import_evaluate_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "REQUESTS", tmp_path / "requests")
    monkeypatch.setattr(run, "RECORDINGS", tmp_path / "recordings")
    path = run.export("trading_planner.v1", "validation")
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == len(VAL) and all(r["system_prompt_sha256"] == run.prompt_for("trading_planner.v1").sha256
                                         for r in rows)
    responses = tmp_path / "responses.jsonl"
    responses.write_text("".join(json.dumps({"request_key": r["request_key"], "output": rule_follower(r["payload"])})
                                 + "\n" for r in rows), encoding="utf-8")
    assert run.import_outputs("trading_planner.v1", responses) == len(VAL)
    report = run.run_label("trading_planner.v1", "validation")
    reference = run.run_label("calibration:rule_follower", "validation", outputs_fn=rule_follower)
    assert report["graded"] == len(VAL) and report["failure_rates"] == reference["failure_rates"]
    # recordings are immutable
    responses.write_text(json.dumps({"request_key": rows[0]["request_key"], "output": "changed"}) + "\n",
                         encoding="utf-8")
    with pytest.raises(ValueError, match="immutable"):
        run.import_outputs("trading_planner.v1", responses)


def test_candidate_prompt_is_not_deployed():
    from trading_agent.testing.harness import runtime_config
    assert runtime_config().prompt_versions["trading_planner"] == "v1"
    assert run.prompt_for("trading_planner.v2-candidate").text.startswith("SYSTEM: TRADING_PLANNER_EN_v2 (CANDIDATE")
