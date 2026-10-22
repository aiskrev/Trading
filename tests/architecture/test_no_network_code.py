"""At this stage no module may open network connections; broker execution stays mock-only."""
import ast
from pathlib import Path

from trading_agent.core.mode import Mode
from trading_agent.execution import registry

SRC = Path(__file__).resolve().parents[2] / "src" / "trading_agent"
NETWORK_MODULES = {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websocket", "websockets",
                   "ftplib", "smtplib", "telnetlib", "paramiko", "grpc"}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module.split(".")[0]


def test_no_network_libraries_imported_anywhere():
    hits = sorted({f"{p.relative_to(SRC)}:{m}" for p in SRC.rglob("*.py") for m in _imports(p) if m in NETWORK_MODULES})
    assert hits == []


def test_broker_execution_is_mock_only():
    assert registry.permitted_adapters() == {Mode.BACKTEST: "SimulatedBroker"}
    names = {p.stem for p in (SRC / "execution").glob("*.py")}
    assert names == {"__init__", "ports", "registry", "simulated_broker", "cost_model"}
