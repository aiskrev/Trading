"""Read-only data access for the control room. Nothing in this module writes a file or reaches the network.

Every function takes a `Workspace` and returns plain JSON-able data. Names that come from the browser are checked
against `SAFE_NAME` and resolved under a fixed base directory; nothing else from the browser becomes a path.
"""
import hashlib
import json
import platform
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
GENESIS = "0" * 64
MAX_TEXT = 400_000          # largest file or log the API will return
DOC_ROOTS = ("docs", "prompts", "config", "requirements", "evals/planner_quality")
DOC_FILES = ("README.md", "CLAUDE.md", "CODEOWNERS", "ui/README.md")
DOC_SUFFIXES = (".md", ".yaml", ".yml", ".json", ".txt", "")


class NotFound(Exception):
    pass


class Workspace:
    def __init__(self, root: Path, runs_dir: Optional[Path] = None) -> None:
        self.root = Path(root).resolve()
        self.runs = Path(runs_dir).resolve() if runs_dir else self.root / "runs"

    def under(self, path: Path, base: Path) -> bool:
        try:
            path.resolve().relative_to(base.resolve())
            return True
        except (ValueError, OSError):
            return False


# ------------------------------------------------------------------ small helpers
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def read_text(path: Path, limit: int = MAX_TEXT) -> str:
    data = path.read_bytes()[:limit + 1]
    text = data.decode("utf-8", errors="replace")
    return text[:limit] + ("\n... [cut]" if len(data) > limit else "")


def read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(ws: Workspace, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(ws.root), capture_output=True, text=True, timeout=10,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def resolve_approved(ws: Workspace, rel: str) -> Optional[Path]:
    """Approval entries name files relative to the project root, or (risk policy files) relative to config/."""
    for candidate in (ws.root / rel, ws.root / "config" / rel):
        if candidate.is_file() and ws.under(candidate, ws.root):
            return candidate
    return None


def file_status(ws: Workspace, rel: str, expected: str) -> str:
    path = resolve_approved(ws, rel)
    if path is None:
        return "missing"
    return "ok" if sha256_file(path) == expected else "changed"


# ------------------------------------------------------------------ overview and governance
def approvals(ws: Workspace) -> List[Dict[str, Any]]:
    """Every approval, with live hash checks: valid, superseded by a later approval, or voided."""
    raw = (read_yaml(ws.root / "config" / "approvals.yaml") or {}).get("approvals", [])
    out: List[Dict[str, Any]] = []
    for i, a in enumerate(raw):
        files = []
        for rel, digest in (a.get("files") or {}).items():
            status = file_status(ws, rel, str(digest))
            superseded_by = None
            if status != "ok":
                path = resolve_approved(ws, rel)
                current = sha256_file(path) if status == "changed" and path else None
                for b in raw[i + 1:]:
                    if current and (b.get("files") or {}).get(rel) == current:
                        superseded_by = b.get("id")
                        break
            files.append({"path": rel, "sha256": str(digest)[:12], "status": status, "superseded_by": superseded_by})
        bad = [f for f in files if f["status"] != "ok"]
        if not bad:
            state = "valid"
        elif all(f["superseded_by"] for f in bad):
            state = "superseded"
        else:
            state = "voided"
        out.append({"id": a.get("id"), "date": str(a.get("date")), "approver": a.get("approver"),
                    "statement": a.get("statement"), "modes": a.get("modes", []),
                    "not_approved_for": a.get("not_approved_for", []), "code_scope": " ".join(str(a.get("code_scope", "")).split()),
                    "files": files, "state": state})
    return list(reversed(out))


def overview(ws: Workspace) -> Dict[str, Any]:
    runtime = read_yaml(ws.root / "config" / "runtime.backtest.yaml") or {}
    try:
        from trading_agent.core.mode import PERMITTED_MODES_THIS_STAGE
        permitted = sorted(m.value for m in PERMITTED_MODES_THIS_STAGE)
    except Exception:
        permitted = []
    kill_files = [str(p.relative_to(ws.root)) for p in (ws.root / "ops" / "KILL_SWITCH",) if p.exists()]
    for run in _run_dirs(ws):
        if (run / "KILL_SWITCH").exists():
            kill_files.append(f"runs/{run.name}/KILL_SWITCH")
    appr = approvals(ws)
    checks_cache = read_json(ws.runs / "ui" / "last_checks.json") or {}
    return {
        "mode": runtime.get("mode"), "permitted_modes": permitted,
        "versions": {k: runtime.get(k) for k in ("strategy_version", "risk_policy_version", "model_version",
                                                  "deployment_id", "code_commit")},
        "prompt_versions": runtime.get("prompt_versions", {}),
        "kill_switch": {"active": bool(kill_files), "files": kill_files},
        "approvals": {"total": len(appr), "valid": sum(a["state"] == "valid" for a in appr),
                      "superseded": sum(a["state"] == "superseded" for a in appr),
                      "voided": sum(a["state"] == "voided" for a in appr)},
        "git": {"branch": git(ws, "rev-parse", "--abbrev-ref", "HEAD"), "commit": git(ws, "log", "-1", "--format=%h %s"),
                "dirty_files": len((git(ws, "status", "--porcelain") or "").splitlines()),
                "ahead_behind": git(ws, "rev-list", "--left-right", "--count", "@{u}...HEAD")},
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "root": str(ws.root),
                        "runs_dir": str(ws.runs)},
        "checks": checks_cache,
        "counts": {"runs": len(_run_dirs(ws)), "datasets": len(datasets(ws)), "backtest_reports": len(backtests(ws))},
    }


def policy(ws: Workspace) -> Dict[str, Any]:
    runtime = read_yaml(ws.root / "config" / "runtime.backtest.yaml") or {}
    version = str(runtime.get("risk_policy_version", "")).replace("-", "_")
    pol_path = ws.root / "config" / f"{version}.yaml"
    pol = read_yaml(pol_path) or {}
    strategies = []
    for p in sorted((ws.root / "config" / "strategies").glob("*.yaml")):
        strategies.append({"file": p.name, "content": read_yaml(p)})
    return {"active_file": pol_path.name, "policy": pol, "universe": read_yaml(ws.root / "config" / "universe.v1.yaml"),
            "restricted": read_yaml(ws.root / "config" / "restricted.v1.yaml"),
            "ops": read_yaml(ws.root / "config" / "ops_policy.v1.yaml"),
            "execution_assumptions": read_yaml(ws.root / "config" / "execution_assumptions.v1.yaml"),
            "strategies": strategies}


def docs_index(ws: Workspace) -> List[Dict[str, str]]:
    items: List[Tuple[str, str]] = []
    for rel in DOC_FILES:
        if (ws.root / rel).is_file():
            items.append((rel, "root"))
    for base in DOC_ROOTS:
        d = ws.root / base
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix in (".md", ".yaml", ".yml") and "frozen" not in p.parts \
                    and "reports" not in p.parts and "requests" not in p.parts and ws.under(p, ws.root):
                items.append((p.relative_to(ws.root).as_posix(), base.split("/")[0]))
    return [{"path": rel, "group": grp} for rel, grp in items]


def doc(ws: Workspace, rel: str) -> Dict[str, str]:
    allowed = {d["path"] for d in docs_index(ws)}
    if rel not in allowed:
        raise NotFound(rel)
    return {"path": rel, "text": read_text(ws.root / rel)}


# ------------------------------------------------------------------ runs and decision packets
def _run_dirs(ws: Workspace) -> List[Path]:
    if not ws.runs.is_dir():
        return []
    return sorted((p for p in ws.runs.iterdir()
                   if p.is_dir() and SAFE_NAME.match(p.name) and p.name != "ui"
                   and ((p / "packets.jsonl").exists() or (p / "ledger.jsonl").exists() or (p / "manifest.json").exists())),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _run_path(ws: Workspace, name: str) -> Path:
    if not SAFE_NAME.match(name or ""):
        raise NotFound(name)
    path = ws.runs / name
    if not path.is_dir() or not ws.under(path, ws.runs):
        raise NotFound(name)
    return path


def _count_lines(path: Path) -> int:
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return 0


def runs(ws: Workspace) -> List[Dict[str, Any]]:
    out = []
    for p in _run_dirs(ws):
        manifest = read_json(p / "manifest.json") or {}
        out.append({"name": p.name, "run_id": manifest.get("run_id"), "modified": iso(p.stat().st_mtime),
                    "packets": _count_lines(p / "packets.jsonl"), "ledger_records": _count_lines(p / "ledger.jsonl"),
                    "segments": len(manifest.get("segments", [])), "recoveries": len(manifest.get("recoveries", []))})
    return out


def verify_chain(path: Path) -> Dict[str, Any]:
    """Read-only check of a hash-chained JSONL file in the `ChainedLog` format (seq, prev_hash, record_hash, body)."""
    from trading_agent.core.hashing import sha256_hex
    if not path.exists():
        return {"file": path.name, "exists": False}
    lines = path.read_bytes().split(b"\n")
    complete, tail = lines[:-1], lines[-1]
    head, error = GENESIS, None
    for i, line in enumerate(complete):
        try:
            row = json.loads(line)
            expected = sha256_hex({"seq": i, "prev_hash": head, "body": row["body"]})
            if row.get("seq") != i or row.get("prev_hash") != head or row.get("record_hash") != expected:
                error = f"hash chain broken at line {i}"
                break
            head = expected
        except (ValueError, KeyError, TypeError):
            error = f"unreadable line {i}"
            break
    return {"file": path.name, "exists": True, "ok": error is None, "entries": len(complete), "error": error,
            "torn_tail": bool(tail.strip()), "head": head[:16]}


def verify_ledger(path: Path) -> Dict[str, Any]:
    """Read-only check of the audit ledger (its records hash their own content)."""
    from trading_agent.audit.ledger import AuditLedger
    from trading_agent.contracts.audit import AuditRecord
    if not path.exists():
        return {"file": path.name, "exists": False}
    lines = path.read_bytes().split(b"\n")
    complete, tail = lines[:-1], lines[-1]
    try:
        records = [AuditRecord.model_validate(json.loads(l)) for l in complete if l.strip()]
        ok = AuditLedger.verify(records)
        error = None if ok else "hash chain broken"
    except Exception as exc:  # unreadable or invalid record
        records, ok, error = [], False, f"unreadable record ({type(exc).__name__})"
    return {"file": path.name, "exists": True, "ok": ok, "entries": len(records), "error": error,
            "torn_tail": bool(tail.strip()), "head": records[-1].record_hash[:16] if records else ""}


def _equity(body: Dict[str, Any]) -> Optional[float]:
    account, market = body.get("account"), body.get("market") or {}
    if not account:
        return None
    total = Decimal(str(account["cash"]))
    for sym, pos in (account.get("positions") or {}).items():
        quote = (market.get("quotes") or {}).get(sym)
        if not quote:
            return None
        total += Decimal(str(pos["quantity"])) * (Decimal(str(quote["bid"])) + Decimal(str(quote["ask"]))) / 2
    return float(total)


def _packet_rows(path: Path) -> List[Dict[str, Any]]:
    rows = []
    if not path.exists():
        return rows
    for i, line in enumerate(path.read_bytes().split(b"\n")[:-1]):
        try:
            body = json.loads(line)["body"]
        except (ValueError, KeyError, TypeError):
            continue
        prop, risk = body.get("proposal") or {}, body.get("risk_decision") or {}
        rows.append({
            "i": i, "at": body.get("at"), "segment": body.get("run_segment"), "session": body.get("session_id"),
            "outcome": body.get("outcome"), "state": body.get("state_after"),
            "decision": prop.get("decision"), "symbol": prop.get("symbol"), "side": prop.get("side"),
            "reason": prop.get("reason_code") or "",
            "verdict": risk.get("verdict") if prop.get("decision") == "TRADE" else None,   # a NO_TRADE needs no risk verdict
            "denied_by": ([r.get("reason_code") for r in risk.get("rule_results", []) if not r.get("passed", True)][:4]
                          if prop.get("decision") == "TRADE" else []),
            "fills": len(body.get("fills") or []), "errors": len(body.get("errors") or []),
            "latency_ms": body.get("signal_to_proposal_ms"), "equity": _equity(body),
        })
    return rows


def run_detail(ws: Workspace, name: str) -> Dict[str, Any]:
    p = _run_path(ws, name)
    manifest = read_json(p / "manifest.json") or {}
    rows = _packet_rows(p / "packets.jsonl")
    metrics = []
    sessions = sorted({r["session"] for r in rows if r["session"]})
    try:
        from trading_agent.contracts.decision_packet import DecisionPacket
        from trading_agent.orchestration.session_metrics import compute
        packets = []
        for line in (p / "packets.jsonl").read_bytes().split(b"\n")[:-1]:
            packets.append(DecisionPacket.model_validate(json.loads(line)["body"]))
        for s in sessions:
            metrics.append(json.loads(compute(packets, s, manifest).model_dump_json()))
    except Exception as exc:
        metrics = [{"error": f"metrics unavailable ({type(exc).__name__})"}]
    return {
        "name": name, "manifest": manifest, "sessions": sessions, "metrics": metrics,
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
        "chains": [verify_chain(p / "packets.jsonl"), verify_ledger(p / "ledger.jsonl"),
                   verify_chain(p / "idempotency.jsonl")],
        "broker_book": read_json(p / "broker_book.json"),
        "kill_switch": (p / "KILL_SWITCH").exists(),
        "series": [{"i": r["i"], "at": r["at"], "equity": r["equity"], "state": r["state"]} for r in rows],
    }


def packets(ws: Workspace, name: str, outcome: str = "", limit: int = 500, offset: int = 0) -> Dict[str, Any]:
    p = _run_path(ws, name)
    rows = _packet_rows(p / "packets.jsonl")
    if outcome:
        rows = [r for r in rows if r["outcome"] == outcome]
    limit = max(1, min(int(limit), 2000))
    return {"total": len(rows), "offset": offset, "rows": rows[offset:offset + limit]}


def packet(ws: Workspace, name: str, index: int) -> Dict[str, Any]:
    p = _run_path(ws, name)
    lines = (p / "packets.jsonl").read_bytes().split(b"\n")[:-1] if (p / "packets.jsonl").exists() else []
    if not (0 <= index < len(lines)):
        raise NotFound(str(index))
    return json.loads(lines[index])["body"]


# ------------------------------------------------------------------ backtests and datasets
def datasets(ws: Workspace) -> List[Dict[str, Any]]:
    out = []
    base = ws.root / "data" / "datasets"
    if base.is_dir():
        for d in sorted(p for p in base.iterdir() if p.is_dir() and SAFE_NAME.match(p.name)):
            manifest = read_yaml(d / "manifest.yaml") or {}
            out.append({"id": d.name, "synthetic": manifest.get("synthetic"), "description": manifest.get("description"),
                        "symbols": manifest.get("symbols"), "manifest": manifest})
    return out


def backtests(ws: Workspace) -> List[Dict[str, Any]]:
    out = []
    base = ws.root / "reports" / "backtests"
    if base.is_dir():
        for d in sorted(p for p in base.iterdir() if p.is_dir() and SAFE_NAME.match(p.name)):
            for f in sorted(d.glob("*.md")):
                out.append({"dataset": d.name, "report": f.stem, "modified": iso(f.stat().st_mtime),
                            "path": f"{d.name}/{f.stem}"})
    return out


def backtest(ws: Workspace, dataset: str, report: str) -> Dict[str, Any]:
    if not (SAFE_NAME.match(dataset or "") and SAFE_NAME.match(report or "")):
        raise NotFound("name")
    base = ws.root / "reports" / "backtests" / dataset
    md, js = base / f"{report}.md", base / f"{report}.json"
    if not md.is_file() or not ws.under(md, ws.root / "reports"):
        raise NotFound(report)
    return {"dataset": dataset, "report": report, "markdown": read_text(md), "data": read_json(js)}


# ------------------------------------------------------------------ model and planner evaluation
def prompts(ws: Workspace) -> List[Dict[str, Any]]:
    out = []
    for folder, status in (("prompts", "released"), ("prompts/candidates", "candidate (offline only)")):
        d = ws.root / folder
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                out.append({"file": f"{folder}/{p.name}", "status": status, "sha256": sha256_file(p)[:16],
                            "chars": len(p.read_text(encoding="utf-8"))})
    return out


def model_info(ws: Workspace) -> Dict[str, Any]:
    script = (ws.root / "scripts" / "run_model_eval.py")
    text = script.read_text(encoding="utf-8") if script.is_file() else ""

    def const(name: str) -> Optional[str]:
        m = re.search(rf'^{name}\s*=\s*"?([^"\n#]+?)"?\s*(?:#.*)?$', text, re.M)
        return m.group(1) if m else None
    rec_dir = ws.root / "evals" / "planner_quality" / "recordings"
    provenance = []
    if rec_dir.is_dir():
        for f in sorted(rec_dir.glob("*.provenance.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines()[-20:]:
                try:
                    provenance.append(json.loads(line))
                except ValueError:
                    pass
    recordings = {f.stem: len(read_json(f) or {}) for f in sorted(rec_dir.glob("*.json"))
                  if not f.name.endswith(".provenance.jsonl")} if rec_dir.is_dir() else {}
    return {"model": const("MODEL"), "max_tokens": const("MAX_TOKENS"), "recordings": recordings,
            "provenance": provenance, "prompts": prompts(ws),
            "commands": [
                {"label": "smoke test (3 requests)",
                 "cmd": "cd ~/trading-agent && read -rs TRADING_VAULT_TOKEN && export TRADING_VAULT_TOKEN\n"
                        "~/venvs/trading/bin/python scripts/run_model_eval.py trading_planner.v1 --split development --limit 3"},
                {"label": "full development run",
                 "cmd": "~/venvs/trading/bin/python scripts/run_model_eval.py trading_planner.v1 --split development --all"},
                {"label": "candidate vs baseline (validation)",
                 "cmd": "~/venvs/trading/bin/python scripts/run_model_eval.py trading_planner.v2-candidate --split validation "
                        "--all --baseline trading_planner.v1"},
                {"label": "check the key is in the vault (names only)",
                 "cmd": "~/venvs/trading/bin/python scripts/vault.py list"}]}


def eval_reports(ws: Workspace) -> List[Dict[str, Any]]:
    out = []
    base = ws.root / "evals" / "planner_quality" / "reports"
    if base.is_dir():
        for d in sorted(p for p in base.iterdir() if p.is_dir()):
            for f in sorted(d.glob("*.json")):
                data = read_json(f) or {}
                out.append({"label": data.get("label", d.name), "split": data.get("split", f.stem),
                            "set_version": data.get("set_version"), "graded": data.get("graded"), "n": data.get("n"),
                            "any_failure_rate": data.get("any_failure_rate"), "id": f"{d.name}/{f.stem}"})
    return out


def eval_report(ws: Workspace, report_id: str) -> Dict[str, Any]:
    parts = (report_id or "").split("/")
    if len(parts) != 2 or not all(SAFE_NAME.match(p) for p in parts):
        raise NotFound(report_id)
    path = ws.root / "evals" / "planner_quality" / "reports" / parts[0] / f"{parts[1]}.json"
    data = read_json(path)
    if data is None or not ws.under(path, ws.root):
        raise NotFound(report_id)
    data.pop("scenarios", None)
    return data


def scenarios(ws: Workspace, split: str, kind: str = "") -> List[Dict[str, Any]]:
    """Development and validation scenarios only: the sealed holdout is never exposed here."""
    if split not in ("development", "validation"):
        raise NotFound(split)
    from evals.planner_quality import scenarios as sc
    rows = sc.load(split)
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return [{"id": r["scenario_id"], "kind": r["kind"], "date": r["date"], "expect": r["expect"],
             "traps": r["traps"]} for r in rows]


def scenario(ws: Workspace, split: str, scenario_id: str) -> Dict[str, Any]:
    if split not in ("development", "validation") or not SAFE_NAME.match(scenario_id or ""):
        raise NotFound(scenario_id)
    from evals.planner_quality import run as ev
    from evals.planner_quality import scenarios as sc
    from evals.planner_quality.taxonomy import grade
    row = next((r for r in sc.load(split) if r["scenario_id"] == scenario_id), None)
    if row is None:
        raise NotFound(scenario_id)
    outputs = []
    rec_dir = ws.root / "evals" / "planner_quality" / "recordings"
    if rec_dir.is_dir():
        for f in sorted(rec_dir.glob("*.json")):
            if f.name.endswith(".provenance.jsonl"):
                continue
            label = f.stem
            try:
                key = ev._key(ev.prompt_for(label), row["payload"])
            except Exception:
                continue
            recorded = (read_json(f) or {}).get(key)
            if recorded is not None:
                g = grade(row, recorded)
                outputs.append({"label": label, "output": recorded, "failures": [x.value for x in g.failures],
                                "decision": g.decision})
    payload = {k: v for k, v in row["payload"].items() if k != "output_schema"}
    return {"id": scenario_id, "kind": row["kind"], "date": row["date"], "expect": row["expect"], "traps": row["traps"],
            "payload": payload, "recorded_outputs": outputs}
