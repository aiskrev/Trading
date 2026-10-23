"""The only things the control room can start: a fixed list of local commands. Nothing from the browser is a command.

A browser request names a job key (and, for the backtest, a dataset id that must exist under data/datasets). The argv
is built here. The child process gets a cleaned environment (anything that looks like a token, key or password is
dropped) and its output is redacted before it is stored or sent. One job at a time.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ui.data import SAFE_NAME, Workspace, iso

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SECRET_ENV = re.compile(r"(?i)(token|secret|passw|passphrase|api[_-]?key|credential|private)")
MAX_OUTPUT_CHARS = 200_000
TIMEOUT_S = {"tests": 900, "backtest": 600}
DEFAULT_TIMEOUT_S = 300

CHECK_KEYS = ("tests", "imports", "schemas", "risk_lock", "sbom", "evals")
ACTION_KEYS = ("backtest", "demo_run")

LABELS = {
    "tests": "Tests (pytest)", "imports": "Module boundaries (import-linter)", "schemas": "JSON Schema drift",
    "risk_lock": "Released risk config unchanged", "sbom": "SBOM matches the lock", "evals": "Guardrail evals",
    "backtest": "Backtest on a dataset", "demo_run": "Create a demo live run (mock feeds, BACKTEST)",
}


def clean_env(root: Path) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
    paths = [str(root / "src")]
    if (root / ".deps").is_dir():
        paths.append(str(root / ".deps"))
    if os.environ.get("PYTHONPATH"):               # keep the caller's own import path (e.g. a dependency folder elsewhere)
        paths.append(os.environ["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def build_argv(root: Path, key: str, dataset: Optional[str] = None, runs_dir: Optional[Path] = None) -> List[str]:
    py = sys.executable
    if key == "tests":
        return [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts="]
    if key == "imports":
        return [py, "-c", "import sys; from importlinter.cli import lint_imports_command as c; sys.exit(c())"]
    if key == "schemas":
        return [py, "scripts/export_schemas.py", "--check"]
    if key == "risk_lock":
        return [py, "scripts/lock_risk_config.py", "--check"]
    if key == "sbom":
        return [py, "scripts/make_sbom.py", "--check"]
    if key == "evals":
        return [py, "-m", "evals.runner"]
    if key == "backtest":
        if not dataset or not SAFE_NAME.match(dataset) or not (root / "data" / "datasets" / dataset).is_dir():
            raise ValueError("unknown dataset")
        return [py, "-m", "trading_agent.backtest.run_dataset", "--dataset-version", dataset]
    if key == "demo_run":
        return [py, "-m", "ui.make_demo_run"] + (["--runs-dir", str(runs_dir)] if runs_dir else [])
    raise ValueError("unknown job")


class Job:
    def __init__(self, key: str, argv: List[str], label: str) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.key, self.argv, self.label = key, argv, label
        self.started = time.time()
        self.finished: Optional[float] = None
        self.exit_code: Optional[int] = None
        self.output = ""
        self.truncated = False
        self.state = "running"

    def view(self, tail: int = MAX_OUTPUT_CHARS) -> Dict[str, Any]:
        return {"id": self.id, "key": self.key, "label": self.label, "state": self.state, "exit_code": self.exit_code,
                "started": iso(self.started), "finished": iso(self.finished) if self.finished else None,
                "seconds": round((self.finished or time.time()) - self.started, 1),
                "output": self.output[-tail:], "truncated": self.truncated}


class JobRunner:
    def __init__(self, ws: Workspace) -> None:
        self.ws = ws
        self._lock = threading.Lock()
        self._jobs: Dict[str, Job] = {}
        self._current: Optional[Job] = None

    def start(self, key: str, dataset: Optional[str] = None) -> Job:
        from trading_agent.observability.logging import redact
        if key not in CHECK_KEYS + ACTION_KEYS:
            raise ValueError("unknown job")
        argv = build_argv(self.ws.root, key, dataset, self.ws.runs if key == "demo_run" else None)
        with self._lock:
            if self._current is not None and self._current.state == "running":
                raise RuntimeError("another job is running")
            job = Job(key, argv, LABELS[key] + (f": {dataset}" if key == "backtest" else ""))
            self._jobs[job.id] = job
            self._current = job
        threading.Thread(target=self._run, args=(job, redact), daemon=True).start()
        return job

    def _run(self, job: Job, redact) -> None:
        timeout = TIMEOUT_S.get(job.key, DEFAULT_TIMEOUT_S)
        try:
            proc = subprocess.Popen(job.argv, cwd=str(self.ws.root), env=clean_env(self.ws.root), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                    errors="replace")
            timer = threading.Timer(timeout, proc.kill)
            timer.start()
            try:
                for line in proc.stdout:
                    if len(job.output) < MAX_OUTPUT_CHARS:
                        job.output += redact(ANSI.sub("", line))
                    else:
                        job.truncated = True
                proc.wait()
            finally:
                timer.cancel()
            job.exit_code = proc.returncode
        except OSError as exc:
            job.output += f"could not start: {type(exc).__name__}\n"
            job.exit_code = -1
        job.finished = time.time()
        job.state = "passed" if job.exit_code == 0 else "failed"
        if job.key in CHECK_KEYS:
            self._remember(job)

    def _remember(self, job: Job) -> None:
        path = self.ws.runs / "ui" / "last_checks.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            known = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            known[job.key] = {"state": job.state, "finished": iso(job.finished), "seconds": round(job.finished - job.started, 1),
                              "last_line": next((l for l in reversed(job.output.splitlines()) if l.strip()), "")[:200]}
            path.write_text(json.dumps(known, indent=1), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id) if SAFE_NAME.match(job_id or "") else None

    def recent(self) -> List[Dict[str, Any]]:
        jobs = sorted(self._jobs.values(), key=lambda j: j.started, reverse=True)[:15]
        return [dict(j.view(tail=0), output="") for j in jobs]
