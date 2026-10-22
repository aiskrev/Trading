"""Offline planner-quality evaluation CLI.

  python -m evals.planner_quality.run export   trading_planner.v1 --split development
  python -m evals.planner_quality.run import   trading_planner.v1 responses.jsonl
  python -m evals.planner_quality.run evaluate trading_planner.v1 --split validation --baseline <label>
  python -m evals.planner_quality.run calibrate                      # reference models, dev+validation

Model calls never happen here. `export` writes the exact requests (system prompt + payload +
request_key); run them in an approved environment and `import` the raw outputs.

Holdout rules (do not optimise on the test period):
  * development: iterate freely. validation: compare candidates. holdout: final check only.
  * holdout export/evaluate needs --final AND the label listed in holdout_candidates.yaml
    (committed before looking), and every access is appended to holdout_access.log.
  * recommendations must be justified by development/validation results, never holdout.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "src", ROOT / ".deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import yaml  # noqa: E402

from evals.planner_quality import scenarios as sc  # noqa: E402
from evals.planner_quality.taxonomy import DESCRIPTIONS, Failure, Secondary, grade  # noqa: E402
from trading_agent.planning.prompts import PromptSpec, load_prompt  # noqa: E402
from trading_agent.planning.recorded_llm import request_key  # noqa: E402

HERE = Path(__file__).resolve().parent
REQUESTS = HERE / "requests"
RECORDINGS = HERE / "recordings"
REPORTS = HERE / "reports"
CANDIDATES_FILE = HERE / "holdout_candidates.yaml"
ACCESS_LOG = HERE / "holdout_access.log"
SPLITS = ("development", "validation", "holdout")


class HoldoutSealed(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers
def prompt_for(label: str) -> PromptSpec:
    """'trading_planner.v1' -> prompts/trading_planner.v1.md ; candidates live in prompts/candidates/."""
    prompt_id, version = label.split(".", 1)
    for d in (ROOT / "prompts", ROOT / "prompts" / "candidates"):
        if (d / f"{prompt_id}.{version}.md").exists():
            return load_prompt(d, prompt_id, version)
    raise FileNotFoundError(label)


def guard_holdout(split: str, label: str, final: bool, action: str) -> None:
    if split != "holdout":
        return
    allowed = (yaml.safe_load(CANDIDATES_FILE.read_text(encoding="utf-8")) or {}).get("candidates") or []
    if not final or label not in allowed:
        raise HoldoutSealed(f"holdout is sealed: needs --final and {label!r} in {CANDIDATES_FILE.name}")
    with ACCESS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "action": action, "label": label,
                             "set_version": sc.SET_VERSION}) + "\n")


def _key(prompt: PromptSpec, payload: Dict[str, Any]) -> str:
    return request_key(prompt.prompt_id, prompt.version, prompt.text, payload)


# ------------------------------------------------------------------ export / import
def export(label: str, split: str, final: bool = False) -> Path:
    guard_holdout(split, label, final, "export")
    prompt = prompt_for(label)
    out_dir = REQUESTS / label
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "system_prompt.md").write_bytes(prompt.text.encode("utf-8"))
    path = out_dir / f"{split}.jsonl"
    with path.open("wb") as fh:
        for s in sc.load(split):
            fh.write((json.dumps({"request_key": _key(prompt, s["payload"]), "scenario_id": s["scenario_id"],
                                  "prompt": label, "system_prompt_sha256": prompt.sha256,
                                  "payload": s["payload"]}, sort_keys=True) + "\n").encode("utf-8"))
    return path


def import_outputs(label: str, responses: Path) -> int:
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    path = RECORDINGS / f"{label}.json"
    recs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    n = 0
    for line in responses.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key, output = row["request_key"], row["output"]
        if key in recs and recs[key] != output:
            raise ValueError(f"recording {key[:12]} already exists with a different output (outputs are immutable)")
        recs[key] = output
        n += 1
    path.write_bytes((json.dumps(recs, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    return n


def recorded_outputs(label: str, split: str) -> Dict[str, Optional[str]]:
    prompt = prompt_for(label)
    path = RECORDINGS / f"{label}.json"
    recs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {s["scenario_id"]: recs.get(_key(prompt, s["payload"])) for s in sc.load(split)}


# ------------------------------------------------------------------ evaluation
def evaluate_outputs(label: str, split: str, outputs: Dict[str, Optional[str]]) -> Dict[str, Any]:
    scenarios = sc.load(split)
    graded = [grade(s, outputs.get(s["scenario_id"])) for s in scenarios]
    by_id = {s["scenario_id"]: s for s in scenarios}
    done = [g for g in graded if g.status == "GRADED"]
    rates = {}
    for cat in Failure:
        hits = [g for g in done if cat in g.failures]
        targeted = [g for g in done if cat.value in by_id[g.scenario_id]["traps"]]
        rates[cat.value] = {"count": len(hits), "rate": _r(len(hits), len(done)),
                            "targeted_n": len(targeted),
                            "targeted_rate": _r(sum(cat in g.failures for g in targeted), len(targeted))}
    expect_trade = [g for g in done if by_id[g.scenario_id]["expect"]["decision"] == "TRADE"]
    secondary = {Secondary.OVER_ABSTENTION.value: _r(sum(Secondary.OVER_ABSTENTION in g.secondary for g in expect_trade),
                                                     len(expect_trade)),
                 Secondary.WRONG_TRADE.value: _r(sum(Secondary.WRONG_TRADE in g.secondary for g in expect_trade),
                                                 len(expect_trade))}
    by_kind: Dict[str, Dict[str, int]] = {}
    for g in done:
        k = by_kind.setdefault(by_id[g.scenario_id]["kind"], {"n": 0, "failed": 0})
        k["n"] += 1
        k["failed"] += bool(g.failures)
    clock = [g for g in done if by_id[g.scenario_id]["requires_clock"]]
    return {
        "label": label, "set_version": sc.SET_VERSION, "split": split, "n": len(scenarios), "graded": len(done),
        "not_run": len(scenarios) - len(done), "any_failure_rate": _r(sum(bool(g.failures) for g in done), len(done)),
        "failure_rates": rates, "secondary": secondary, "by_kind": dict(sorted(by_kind.items())),
        "requires_clock_failed": f"{sum(bool(g.failures) for g in clock)}/{len(clock)}",
        "scenarios": [{"id": g.scenario_id, "decision": g.decision, "status": g.status,
                       "failures": [x.value for x in g.failures], "secondary": [x.value for x in g.secondary]}
                      for g in graded],
    }


def compare(new: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    if (new["set_version"], new["split"]) != (base["set_version"], base["split"]):
        raise ValueError("can only compare reports on the same eval set and split")
    b = {s["id"]: s for s in base["scenarios"] if s["status"] == "GRADED"}
    n = {s["id"]: s for s in new["scenarios"] if s["status"] == "GRADED"}
    common = sorted(set(b) & set(n))
    new_f = [(i, c) for i in common for c in n[i]["failures"] if c not in b[i]["failures"]]
    fixed = [(i, c) for i in common for c in b[i]["failures"] if c not in n[i]["failures"]]
    deltas = {c: round(new["failure_rates"][c]["rate"] - base["failure_rates"][c]["rate"], 4) for c in new["failure_rates"]}
    return {"baseline": base["label"], "compared_scenarios": len(common), "rate_deltas": deltas,
            "new_failures": [{"id": i, "failure": c} for i, c in new_f],
            "fixed_failures": [{"id": i, "failure": c} for i, c in fixed],
            "regressed": bool(new_f) and any(v > 0 for v in deltas.values())}


def write_report(report: Dict[str, Any], comparison: Optional[Dict[str, Any]] = None) -> Path:
    out = REPORTS / report["label"].replace(":", "_")
    out.mkdir(parents=True, exist_ok=True)
    full = dict(report, comparison=comparison)
    (out / f"{report['split']}.json").write_bytes((json.dumps(full, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    (out / f"{report['split']}.md").write_bytes(render(report, comparison).encode("utf-8"))
    return out


def render(r: Dict[str, Any], cmp: Optional[Dict[str, Any]] = None) -> str:
    lines = [f"# {r['label']} on {r['set_version']} / {r['split']}", "",
             f"graded {r['graded']}/{r['n']} (not run: {r['not_run']}); scenarios with any failure: "
             f"{r['any_failure_rate']:.1%}; stale-snapshot scenarios needing a clock failed: {r['requires_clock_failed']}",
             "", "| failure | count | rate | targeted rate (n) |" + (" delta vs baseline |" if cmp else ""),
             "|---|---|---|---|" + ("---|" if cmp else "")]
    for c, v in r["failure_rates"].items():
        row = f"| {c} | {v['count']} | {v['rate']:.1%} | {v['targeted_rate']:.1%} ({v['targeted_n']}) |"
        if cmp:
            row += f" {cmp['rate_deltas'][c]:+.1%} |"
        lines.append(row)
    lines += ["", f"over-abstention: {r['secondary']['OVER_ABSTENTION']:.1%} · wrong trade: "
                  f"{r['secondary']['WRONG_TRADE']:.1%} (of scenarios where a trade is correct)", ""]
    if cmp:
        lines += [f"## vs {cmp['baseline']}", f"new failures: {len(cmp['new_failures'])} · fixed: "
                  f"{len(cmp['fixed_failures'])}", ""]
        lines += [f"- NEW {x['id']}: {x['failure']}" for x in cmp["new_failures"][:50]]
    lines += ["", "## by scenario kind", "| kind | failed / n |", "|---|---|"]
    lines += [f"| {k} | {v['failed']}/{v['n']} |" for k, v in r["by_kind"].items()]
    return "\n".join(lines) + "\n"


def _r(a: int, b: int) -> float:
    return round(a / b, 4) if b else 0.0


def run_label(label: str, split: str, final: bool = False,
              outputs_fn: Optional[Callable[[Dict[str, Any]], str]] = None) -> Dict[str, Any]:
    guard_holdout(split, label, final, "evaluate")
    if outputs_fn is not None:
        outputs = {s["scenario_id"]: outputs_fn(s["payload"]) for s in sc.load(split)}
    else:
        outputs = recorded_outputs(label, split)
    return evaluate_outputs(label, split, outputs)


# ------------------------------------------------------------------ CLI
def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export"); e.add_argument("label"); e.add_argument("--split", default="development")
    e.add_argument("--final", action="store_true")
    i = sub.add_parser("import"); i.add_argument("label"); i.add_argument("responses", type=Path)
    v = sub.add_parser("evaluate"); v.add_argument("label"); v.add_argument("--split", default="development")
    v.add_argument("--baseline"); v.add_argument("--final", action="store_true")
    sub.add_parser("calibrate")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        print(export(a.label, a.split, a.final))
    elif a.cmd == "import":
        print(f"imported {import_outputs(a.label, a.responses)} outputs")
    elif a.cmd == "evaluate":
        report = run_label(a.label, a.split, a.final)
        cmp = compare(report, run_label(a.baseline, a.split, a.final)) if a.baseline else None
        print(render(report, cmp))
        write_report(report, cmp)
    elif a.cmd == "calibrate":
        from evals.planner_quality.reference_models import REFERENCE_MODELS
        for split in ("development", "validation"):
            base = None
            for name, fn in REFERENCE_MODELS.items():
                rep = run_label(f"calibration:{name}", split, outputs_fn=fn)
                cmp = compare(rep, base) if base else None
                write_report(rep, cmp)
                base = base or rep
                print(f"{split:<12} {name:<18} any-failure {rep['any_failure_rate']:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
