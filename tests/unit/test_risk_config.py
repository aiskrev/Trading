"""Risk thresholds come only from immutable, versioned, hash-pinned configuration."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from trading_agent.backtest.engine import BacktestEngine
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import RuntimeConfig
from trading_agent.market_data.csv_source import CsvMarketDataSource
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.risk.policy import LOCK_FILE, load_risk_policy
from trading_agent.testing.harness import PASS_JSON, ROOT, Harness, risk_policy, runtime_config, trade_json

CONFIG = ROOT / "config"


@pytest.fixture()
def config_copy(tmp_path) -> Path:
    for name in ("risk_policy.v2.yaml", "restricted.v1.yaml", LOCK_FILE):
        shutil.copy(CONFIG / name, tmp_path / name)
    return tmp_path


def test_released_files_match_lock():
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "lock_risk_config.py"), "--check"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_editing_a_released_policy_is_refused(config_copy):
    path = config_copy / "risk_policy.v2.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace('max_order_notional: "10000"',
                                                             'max_order_notional: "1000000"'), encoding="utf-8")
    with pytest.raises(ConfigError, match="modified after release"):
        load_risk_policy(path)


def test_editing_the_restricted_list_is_refused(config_copy):
    (config_copy / "restricted.v1.yaml").write_text("version: restricted.v1\nsymbols: [ZZZ]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="modified after release"):
        load_risk_policy(config_copy / "risk_policy.v2.yaml")


def test_unpinned_policy_is_refused(config_copy):
    shutil.copy(config_copy / "risk_policy.v2.yaml", config_copy / "risk_policy.v3.yaml")
    with pytest.raises(ConfigError, match="not a released"):
        load_risk_policy(config_copy / "risk_policy.v3.yaml")


def test_file_name_must_match_declared_version(config_copy):
    import yaml
    from trading_agent.risk.policy import file_sha256
    shutil.copy(config_copy / "risk_policy.v2.yaml", config_copy / "risk_policy.v3.yaml")
    lock = yaml.safe_load((config_copy / LOCK_FILE).read_text(encoding="utf-8"))
    lock["files"]["risk_policy.v3.yaml"] = file_sha256(config_copy / "risk_policy.v3.yaml")
    (config_copy / LOCK_FILE).write_text(yaml.safe_dump(lock), encoding="utf-8")
    with pytest.raises(ConfigError, match="does not match declared version"):
        load_risk_policy(config_copy / "risk_policy.v3.yaml")


def test_lock_tool_refuses_to_repin_a_changed_release(tmp_path):
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "src", tmp_path / "src")
    policy = tmp_path / "config" / "risk_policy.v2.yaml"
    policy.write_text(policy.read_text(encoding="utf-8") + "\n# edit\n", encoding="utf-8")
    env_path = str(ROOT / ".deps")
    proc = subprocess.run([sys.executable, str(tmp_path / "scripts" / "lock_risk_config.py"), str(policy)],
                          capture_output=True, text=True, env={"PYTHONPATH": env_path, "SYSTEMROOT": "C:\\Windows"})
    assert proc.returncode == 1 and "refusing" in proc.stdout


def test_every_decision_records_policy_version_and_hash():
    h = Harness(planner_fn=lambda p: trade_json(), critic_fn=lambda p: PASS_JSON)
    d = h.tick().decision
    p = risk_policy()
    assert (d.policy_version, d.policy_sha256) == (p.version, p.source_sha256)
    import yaml
    pinned = yaml.safe_load((CONFIG / LOCK_FILE).read_text(encoding="utf-8"))["files"]["risk_policy.v2.yaml"]
    assert d.policy_sha256 == pinned


def test_runtime_and_policy_versions_must_agree():
    cfg = RuntimeConfig.model_validate({**runtime_config().model_dump(), "risk_policy_version": "risk-policy.v1"})
    with pytest.raises(ConfigError):
        BacktestEngine(cfg, risk_policy(), ScriptedLLM(demo_planner_response), ScriptedLLM(demo_critic_response),
                       primary=CsvMarketDataSource(ROOT / "data" / "fixtures" / "quotes_primary.csv", "primary"))


def test_session_calendar():
    from datetime import datetime, timezone
    s = risk_policy().session
    utc = timezone.utc
    assert s.is_open(datetime(2026, 1, 5, 14, 30, tzinfo=utc))        # 09:30 ET Monday
    assert not s.is_open(datetime(2026, 1, 5, 14, 29, 59, tzinfo=utc))
    assert not s.is_open(datetime(2026, 1, 5, 20, 55, tzinfo=utc))    # 15:55 ET: avoid-close window
    assert s.is_open(datetime(2026, 7, 6, 13, 30, tzinfo=utc))         # 09:30 EDT (DST)
    assert not s.is_open(datetime(2026, 7, 3, 15, 0, tzinfo=utc))      # holiday
    assert not s.is_open(datetime(2026, 1, 11, 15, 0, tzinfo=utc))     # Sunday
