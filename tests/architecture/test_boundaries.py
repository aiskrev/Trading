"""AT-01: module boundaries enforced by import-linter."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_import_linter_contracts_hold():
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT / ".deps")]))
    proc = subprocess.run(
        [sys.executable, "-c", "import sys; from importlinter.cli import lint_imports; "
                               "sys.exit(lint_imports(no_cache=True))"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BROKEN" not in proc.stdout


def test_planner_is_forbidden_from_execution_and_signing():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = text.split('name = "Planner (LLM) never reaches', 1)[1].split("[[tool.importlinter.contracts]]", 1)[0]
    for forbidden in ("trading_agent.execution", "trading_agent.core.signing", "trading_agent.risk",
                      "trading_agent.secret_store"):
        assert f'"{forbidden}"' in block
