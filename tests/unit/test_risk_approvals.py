"""The explicit approval step is enforced: a policy runs only in modes approved for its exact hash."""
import shutil

import pytest
import yaml

from trading_agent.backtest.engine import BacktestEngine
from trading_agent.core.errors import ConfigError
from trading_agent.core.mode import Mode
from trading_agent.market_data.csv_source import CsvMarketDataSource
from trading_agent.planning.demo_strategy import demo_critic_response, demo_planner_response
from trading_agent.planning.recorded_llm import ScriptedLLM
from trading_agent.risk.approvals import APPROVALS_FILE, load_approvals, require_approval
from trading_agent.risk.policy import LOCK_FILE
from trading_agent.testing.harness import ROOT, risk_policy, runtime_config

CONFIG = ROOT / "config"


def _engine(config_dir):
    return BacktestEngine(runtime_config(), risk_policy(), ScriptedLLM(demo_planner_response),
                          ScriptedLLM(demo_critic_response),
                          primary=CsvMarketDataSource(ROOT / "data" / "fixtures" / "quotes_primary.csv", "primary"),
                          config_dir=config_dir)


@pytest.fixture()
def cfg(tmp_path):
    for name in ("risk_policy.v2.yaml", "restricted.v1.yaml", LOCK_FILE, APPROVALS_FILE):
        shutil.copy(CONFIG / name, tmp_path / name)
    return tmp_path


def test_v2_is_approved_for_backtest_only():
    a = require_approval(risk_policy(), Mode.BACKTEST, CONFIG)
    assert a.id == "APR-0001" and a.modes == [Mode.BACKTEST]
    for mode in Mode:
        if mode is not Mode.BACKTEST:
            with pytest.raises(ConfigError, match="no approval"):
                require_approval(risk_policy(), mode, CONFIG)


def test_approval_hashes_match_pinned_release():
    pinned = yaml.safe_load((CONFIG / LOCK_FILE).read_text(encoding="utf-8"))["files"]
    approved = load_approvals(CONFIG)[0].files
    assert all(pinned[name] == digest for name, digest in approved.items())


def test_engine_starts_with_approval():
    assert _engine(CONFIG).approval.id == "APR-0001"


def test_engine_refuses_without_approval(cfg):
    (cfg / APPROVALS_FILE).write_text("approvals: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no approval"):
        _engine(cfg)


def test_approval_for_different_hash_does_not_count(cfg):
    raw = yaml.safe_load((cfg / APPROVALS_FILE).read_text(encoding="utf-8"))
    raw["approvals"][0]["files"]["risk_policy.v2.yaml"] = "0" * 64
    (cfg / APPROVALS_FILE).write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="no approval"):
        _engine(cfg)


def test_duplicate_approval_ids_rejected(cfg):
    raw = yaml.safe_load((cfg / APPROVALS_FILE).read_text(encoding="utf-8"))
    raw["approvals"].append(dict(raw["approvals"][0]))
    (cfg / APPROVALS_FILE).write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate"):
        load_approvals(cfg)
