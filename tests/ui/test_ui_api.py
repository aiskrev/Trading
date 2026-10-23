"""What the control room shows must be true: approvals states, hash-chain checks (read-only!), runs, jobs."""
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ui import data, jobs
from ui.server import make_server

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def runs_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("runs")
    proc = subprocess.run([sys.executable, "-m", "ui.make_demo_run", "--runs-dir", str(d), "--steps", "26"], cwd=str(ROOT),
                          env=jobs.clean_env(ROOT), capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return d


@pytest.fixture()
def ws(runs_dir):
    return data.Workspace(ROOT, runs_dir)


def wait(runner, job, seconds=120):
    end = time.time() + seconds
    while job.state == "running" and time.time() < end:
        time.sleep(0.2)
    return job


# ------------------------------------------------------------------ overview and approvals
def test_overview_reports_the_real_state(ws):
    o = data.overview(ws)
    assert o["mode"] == "BACKTEST" and o["permitted_modes"] == ["BACKTEST"]
    assert o["kill_switch"]["active"] is False
    assert o["approvals"]["total"] >= 8 and o["approvals"]["voided"] == 0
    assert o["counts"]["runs"] == 1 and o["counts"]["datasets"] >= 1


def test_approvals_follow_the_file_hashes(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    f = root / "config" / "policy.yaml"
    f.write_text("limit: 1\n", encoding="utf-8")
    g = root / "tool.py"
    g.write_text("x = 1\n", encoding="utf-8")

    def sha(p):
        return data.sha256_file(p)

    def write(approvals):
        import yaml
        (root / "config" / "approvals.yaml").write_text(yaml.safe_dump({"approvals": approvals}), encoding="utf-8")

    entry = lambda i, files: {"id": f"APR-000{i}", "date": "2026-10-22", "approver": "owner", "statement": "s", "modes": ["BACKTEST"],
                              "files": files, "code_scope": "scope"}
    ws = data.Workspace(root)
    write([entry(1, {"policy.yaml": sha(f), "tool.py": sha(g)})])         # policy.yaml is named relative to config/
    assert [a["state"] for a in data.approvals(ws)] == ["valid"]
    g.write_text("x = 2\n", encoding="utf-8")                              # a file changes after its approval
    a = data.approvals(ws)[0]
    assert a["state"] == "voided" and {x["path"]: x["status"] for x in a["files"]} == {"policy.yaml": "ok", "tool.py": "changed"}
    write([entry(1, {"policy.yaml": sha(f), "tool.py": "0" * 64}), entry(2, {"tool.py": sha(g)})])   # a newer approval covers it
    by_id = {a["id"]: a for a in data.approvals(ws)}
    assert by_id["APR-0002"]["state"] == "valid" and by_id["APR-0001"]["state"] == "superseded"
    assert by_id["APR-0001"]["files"][1]["superseded_by"] == "APR-0002"
    g.unlink()
    assert {a["id"]: a["state"] for a in data.approvals(ws)} == {"APR-0002": "voided", "APR-0001": "voided"}
    assert data.approvals(ws)[0]["files"][0]["status"] == "missing"


def test_the_real_approvals_are_all_valid_or_superseded(ws):
    states = {a["id"]: a["state"] for a in data.approvals(ws)}
    assert "voided" not in states.values(), states


def test_policy_and_docs(ws):
    p = data.policy(ws)
    assert p["policy"]["version"] == "risk-policy.v2" and p["strategies"]
    index = {d["path"] for d in data.docs_index(ws)}
    assert {"docs/roadmap.md", "README.md", "CLAUDE.md"} <= index
    assert not any("frozen" in x or "reports" in x for x in index)
    assert "roadmap" in data.doc(ws, "docs/roadmap.md")["text"].lower()
    for bad in ("../CLAUDE.md", "src/trading_agent/core/mode.py", "scripts/vault.py", "runs/ui/last_checks.json", ""):
        with pytest.raises(data.NotFound):
            data.doc(ws, bad)


# ------------------------------------------------------------------ runs, chains, packets
def test_runs_and_run_detail(ws):
    runs = data.runs(ws)
    assert [r["name"] for r in runs] == ["demo-live"] and runs[0]["packets"] == 26
    d = data.run_detail(ws, "demo-live")
    assert sum(d["outcomes"].values()) == 26 and len(d["series"]) == 26
    assert {c["file"]: c["ok"] for c in d["chains"]} == {"packets.jsonl": True, "ledger.jsonl": True, "idempotency.jsonl": True}
    assert d["metrics"][0]["decisions"] == 26 and d["metrics"][0]["restarts"] == 1
    assert d["manifest"]["recoveries"] and d["series"][0]["equity"] == 100000.0


def test_packet_rows_and_details(ws):
    page = data.packets(ws, "demo-live", limit=10)
    assert page["total"] == 26 and len(page["rows"]) == 10
    approved = data.packets(ws, "demo-live", outcome="APPROVED")
    assert approved["total"] >= 1 and all(r["outcome"] == "APPROVED" for r in approved["rows"])
    assert all(r["verdict"] is None and r["denied_by"] == [] for r in page["rows"] if r["decision"] == "NO_TRADE")
    body = data.packet(ws, "demo-live", approved["rows"][0]["i"])
    assert body["proposal"]["decision"] == "TRADE" and body["risk_decision"]["verdict"] in ("APPROVE", "REDUCE")
    assert "signature" not in json.dumps(body)                                   # never stored, so never shown
    for bad in (-1, 26, 10 ** 6):
        with pytest.raises(data.NotFound):
            data.packet(ws, "demo-live", bad)
    for bad in ("..", "x/y", "nope", "", "a" * 80):
        with pytest.raises(data.NotFound):
            data.run_detail(ws, bad)


def test_chain_checks_catch_tampering_and_never_modify_a_file(ws, tmp_path):
    src = ws.runs / "demo-live"
    work = tmp_path / "copy"
    shutil.copytree(src, work)
    ws2 = data.Workspace(ROOT, tmp_path)
    before = {p.name: p.read_bytes() for p in work.iterdir()}
    # 1. intact
    d = data.run_detail(ws2, "copy")
    assert all(c["ok"] for c in d["chains"])
    # 2. a torn last line is reported, and the file is left exactly as it was (the real loader would move it aside)
    packets = work / "packets.jsonl"
    packets.write_bytes(packets.read_bytes() + b'{"seq": 26, "prev_hash": "ab')
    torn_bytes = packets.read_bytes()
    chain = data.verify_chain(packets)
    assert chain["ok"] and chain["torn_tail"] and chain["entries"] == 26
    assert packets.read_bytes() == torn_bytes and not (work / "packets.jsonl.torn").exists()
    # 3. a changed byte inside an earlier record breaks the chain
    packets.write_bytes(before["packets.jsonl"].replace(b'"NORMAL"', b'"HALTED"', 1))
    broken = data.verify_chain(packets)
    assert not broken["ok"] and "broken" in broken["error"]
    ledger = work / "ledger.jsonl"
    ledger.write_bytes(before["ledger.jsonl"].replace(b'"NORMAL"', b'"HALTED"', 1))
    assert not data.verify_ledger(ledger)["ok"]
    # nothing else was touched
    assert {p.name for p in work.iterdir()} == set(before)
    for name in ("manifest.json", "broker_book.json", "idempotency.jsonl"):
        assert (work / name).read_bytes() == before[name]


def test_verifying_the_real_demo_run_does_not_change_it(ws):
    src = ws.runs / "demo-live"
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in src.iterdir()}
    data.run_detail(ws, "demo-live")
    data.packets(ws, "demo-live")
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in src.iterdir()}
    assert after == before


# ------------------------------------------------------------------ evaluation views
def test_scenarios_and_evals(ws):
    dev = data.scenarios(ws, "development")
    assert len(dev) == 153 and {s["kind"] for s in dev} >= {"clean_entry", "stale_snapshot"}
    assert len(data.scenarios(ws, "validation", "clean_entry")) == 3
    one = data.scenario(ws, "development", dev[0]["id"])
    assert "output_schema" not in one["payload"] and "evaluation_time" in one["payload"] and one["recorded_outputs"] == []
    for split in ("holdout", "test", ""):
        with pytest.raises(data.NotFound):
            data.scenarios(ws, split)
    with pytest.raises(data.NotFound):
        data.scenario(ws, "holdout", dev[0]["id"])
    reports = data.eval_reports(ws)
    assert reports and all(r["set_version"] == "planner-eval.v2" for r in reports)
    rep = data.eval_report(ws, reports[0]["id"])
    assert "scenarios" not in rep and "failure_rates" in rep


def test_model_info_never_exposes_a_secret(ws):
    info = data.model_info(ws)
    assert info["model"] == "claude-sonnet-5-5" and info["max_tokens"] == "6000"
    text = json.dumps(info)
    assert "sk-ant" not in text and "TRADING_VAULT_TOKEN" in text          # the command text names the variable, never a value
    assert {p["status"] for p in info["prompts"]} == {"released", "candidate (offline only)"}


# ------------------------------------------------------------------ jobs
def test_a_check_job_runs_records_its_result_and_only_one_runs_at_a_time(ws):
    runner = jobs.JobRunner(ws)
    first = runner.start("evals")
    with pytest.raises(RuntimeError):
        runner.start("sbom")                                                  # one at a time
    wait(runner, first)
    assert first.state == "passed" and first.exit_code == 0 and "19/19 eval cases passed" in first.output
    second = wait(runner, runner.start("sbom"))
    assert second.state == "passed" and "SBOM matches the lock" in second.output
    saved = json.loads((ws.runs / "ui" / "last_checks.json").read_text(encoding="utf-8"))
    assert saved["evals"]["state"] == "passed" and saved["sbom"]["state"] == "passed"
    assert data.overview(ws)["checks"]["sbom"]["state"] == "passed"
    assert [j["key"] for j in runner.recent()][:2] == ["sbom", "evals"]
    assert runner.get("../x") is None and runner.get("unknown") is None


def test_a_failing_command_is_reported_as_failed(ws, monkeypatch):
    monkeypatch.setattr(jobs, "build_argv", lambda *a, **k: [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
    runner = jobs.JobRunner(ws)
    job = wait(runner, runner.start("sbom"))
    assert job.state == "failed" and job.exit_code == 3 and "boom" in job.output


def test_job_output_is_redacted(ws, monkeypatch):
    secret = "sk-" + "ant-api03-" + "A" * 30
    monkeypatch.setattr(jobs, "build_argv", lambda *a, **k: [sys.executable, "-c", f"print('token: {secret}'); print('api_key=hunter2hunter2')"])
    runner = jobs.JobRunner(ws)
    job = wait(runner, runner.start("sbom"))
    assert secret not in job.output and "hunter2hunter2" not in job.output and "[REDACTED]" in job.output


def test_a_backtest_job_needs_an_existing_dataset(ws):
    runner = jobs.JobRunner(ws)
    for bad in (None, "", "../x", "nope.v1", "a b"):
        with pytest.raises(ValueError):
            runner.start("backtest", bad)


def test_the_demo_run_job_refuses_to_delete_anything_that_is_not_a_run(tmp_path):
    keep = tmp_path / "demo-live"
    keep.mkdir()
    (keep / "precious.txt").write_text("keep me", encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "ui.make_demo_run", "--runs-dir", str(tmp_path)], cwd=str(ROOT), env=jobs.clean_env(ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and (keep / "precious.txt").read_text(encoding="utf-8") == "keep me"
    for name in ("..", "../x", "a/b", "ui", ".hidden"):
        bad = subprocess.run([sys.executable, "-m", "ui.make_demo_run", "--runs-dir", str(tmp_path), "--name", name], cwd=str(ROOT),
                             env=jobs.clean_env(ROOT), capture_output=True, text=True, timeout=60)
        assert bad.returncode == 2, name


# ------------------------------------------------------------------ end to end over HTTP
def test_a_job_started_over_http_completes(runs_dir):
    import http.client
    srv = make_server(ROOT, runs_dir, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def req(method, path, body=None, token=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=30)
            headers = {"Host": f"127.0.0.1:{srv.port}", "Content-Type": "application/json"}
            if token:
                headers["X-UI-Token"] = token
            c.request(method, path, body=body, headers=headers)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")
        status, session = req("GET", "/api/session")
        assert status == 200 and len(session["token"]) >= 24
        status, job = req("POST", "/api/jobs", json.dumps({"key": "sbom"}), session["token"])
        assert status == 202 and job["state"] == "running"
        for _ in range(100):
            status, view = req("GET", "/api/jobs/" + job["id"])
            if view["state"] != "running":
                break
            time.sleep(0.2)
        assert view["state"] == "passed" and "SBOM matches" in view["output"]
        assert req("GET", "/api/jobs/not-a-job")[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()
